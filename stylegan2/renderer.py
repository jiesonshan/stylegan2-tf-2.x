import numpy as np
import tensorflow as tf

from commons.utils import lerp
from commons.custom_layers import LabelEmbedding, Dense, Bias, LeakyReLU, Upscale2D, ModulatedConv2D, Noise


class ToRGB(tf.keras.layers.Layer):
    def __init__(self, in_channel, **kwargs):
        super(ToRGB, self).__init__(**kwargs)
        self.in_channel = in_channel

        self.mod_conv = ModulatedConv2D(is_up=False, do_demod=False, in_channel=self.in_channel, fmaps=3,
                                        kernel=1, gain=1.0, lrmul=1.0, name='mod_conv')
        self.apply_bias = Bias(lrmul=1.0, name='bias')

    def call(self, inputs, training=None, mask=None):
        x, y, w = inputs

        t = self.mod_conv([x, w])
        t = self.apply_bias(t)

        return t if y is None else y + t


class Mapping(tf.keras.layers.Layer):
    def __init__(self, x_dim, w_dim, n_mapping, n_broadcast, **kwargs):
        super(Mapping, self).__init__(**kwargs)
        self.x_dim = x_dim
        self.w_dim = w_dim
        self.n_mapping = n_mapping
        self.n_broadcast = n_broadcast
        self.gain = 1.0
        self.lrmul = 0.01

        self.x_embedding = LabelEmbedding(embed_dim=512, name='x_embedding')

        self.norm = tf.keras.layers.Lambda(lambda x: x * tf.math.rsqrt(tf.reduce_mean(tf.square(x), axis=1, keepdims=True) + 1e-8))
        self.broadcast = tf.keras.layers.Lambda(lambda x: tf.tile(x[:, np.newaxis], [1, self.n_broadcast, 1]))

        self.dense_layers = list()
        self.bias_layers = list()
        self.act_layers = list()
        for ii in range(self.n_mapping):
            self.dense_layers.append(Dense(w_dim, gain=self.gain, lrmul=self.lrmul, name='dense_{:d}'.format(ii)))
            self.bias_layers.append(Bias(lrmul=self.lrmul, name='bias_{:d}'.format(ii)))
            self.act_layers.append(LeakyReLU(name='lrelu_{:d}'.format(ii)))

    def call(self, inputs, training=None, mask=None):
        # run through embedding layer once to encode onehot vector
        x = self.x_embedding(inputs)

        # normalize inputs
        x = self.norm(x)

        # apply mapping blocks
        for dense, apply_bias, apply_act in zip(self.dense_layers, self.bias_layers, self.act_layers):
            x = dense(x)
            x = apply_bias(x)
            x = apply_act(x)

        x = self.broadcast(x)
        return x


class SynthesisConstBlock(tf.keras.layers.Layer):
    def __init__(self, fmaps, res, **kwargs):
        super(SynthesisConstBlock, self).__init__(**kwargs)
        assert res == 4
        self.res = res
        self.fmaps = fmaps
        self.gain = 1.0
        self.lrmul = 1.0

        # conv block
        self.mod_conv = ModulatedConv2D(is_up=False, do_demod=True, in_channel=self.fmaps, fmaps=self.fmaps,
                                        kernel=3, gain=self.gain, lrmul=self.lrmul, name='mod_conv')
        self.apply_noise = Noise(name='noise')
        self.apply_bias = Bias(lrmul=self.lrmul, name='bias')
        self.apply_act = LeakyReLU(name='lrelu')

    def build(self, input_shape):
        # starting const variable
        # tf 1.15 mean(0.0), std(1.0) default value of tf.initializers.random_normal()
        const_init = tf.random.normal(shape=(1, self.fmaps, self.res, self.res), mean=0.0, stddev=1.0)
        self.const = tf.Variable(const_init, name='const', trainable=True)

    def call(self, inputs, training=None, mask=None):
        w0 = inputs
        batch_size = tf.shape(w0)[0]

        # const block
        x = tf.tile(self.const, [batch_size, 1, 1, 1])

        # conv block
        x = self.mod_conv([x, w0])
        x = self.apply_noise(x)
        x = self.apply_bias(x)
        x = self.apply_act(x)
        return x


class SynthesisBlock(tf.keras.layers.Layer):
    def __init__(self, in_channel, fmaps, res, **kwargs):
        super(SynthesisBlock, self).__init__(**kwargs)
        self.in_channel = in_channel
        self.fmaps = fmaps
        self.res = res
        self.gain = 1.0
        self.lrmul = 1.0

        # conv0 up
        self.mod_conv_0 = ModulatedConv2D(is_up=True, do_demod=True, in_channel=self.in_channel, fmaps=self.fmaps,
                                          kernel=3, gain=self.gain, lrmul=self.lrmul, name='mod_conv_0')
        self.apply_noise_0 = Noise(name='noise_0')
        self.apply_bias_0 = Bias(lrmul=self.lrmul, name='bias_0')
        self.apply_act_0 = LeakyReLU(name='lrelu_0')

        # conv block
        self.mod_conv_1 = ModulatedConv2D(is_up=False, do_demod=True, in_channel=self.fmaps, fmaps=self.fmaps,
                                          kernel=3, gain=self.gain, lrmul=self.lrmul, name='mod_conv_1')
        self.apply_noise_1 = Noise(name='noise_1')
        self.apply_bias_1 = Bias(lrmul=self.lrmul, name='bias_1')
        self.apply_act_1 = LeakyReLU(name='lrelu_1')

    def call(self, inputs, training=None, mask=None):
        x, w0, w1 = inputs

        # conv0 up
        x = self.mod_conv_0([x, w0])
        x = self.apply_noise_0(x)
        x = self.apply_bias_0(x)
        x = self.apply_act_0(x)

        # conv block
        x = self.mod_conv_1([x, w1])
        x = self.apply_noise_1(x)
        x = self.apply_bias_1(x)
        x = self.apply_act_1(x)
        return x


class Synthesis(tf.keras.layers.Layer):
    def __init__(self, w_dim, resolutions, featuremaps, name, **kwargs):
        super(Synthesis, self).__init__(name=name, **kwargs)
        self.w_dim = w_dim
        self.resolutions = resolutions
        self.featuremaps = featuremaps

        # initial layer
        res, n_f = resolutions[0], featuremaps[0]
        self.initial_block = SynthesisConstBlock(fmaps=n_f, res=res, name='{:d}x{:d}/const'.format(res, res))
        self.initial_torgb = ToRGB(in_channel=n_f, name='{:d}x{:d}/ToRGB'.format(res, res))

        # stack generator block with lerp block
        prev_n_f = n_f
        self.blocks = list()
        self.torgbs = list()
        self.upscales = list()
        for res, n_f in zip(self.resolutions[1:], self.featuremaps[1:]):
            self.blocks.append(SynthesisBlock(in_channel=prev_n_f, fmaps=n_f, res=res,
                                              name='{:d}x{:d}/block'.format(res, res)))
            self.upscales.append(Upscale2D())
            self.torgbs.append(ToRGB(in_channel=n_f, name='{:d}x{:d}/ToRGB'.format(res, res)))

            prev_n_f = n_f

    def call(self, inputs, training=None, mask=None):
        w_broadcasted = inputs
        y = None

        # initial layer
        w0, w1 = w_broadcasted[:, 0], w_broadcasted[:, 1]
        x = self.initial_block(w0)
        y = self.initial_torgb([x, y, w1])

        layer_index = 1
        for block, upscale2d, torgb in zip(self.blocks, self.upscales, self.torgbs):
            w0 = w_broadcasted[:, layer_index]
            w1 = w_broadcasted[:, layer_index + 1]
            w2 = w_broadcasted[:, layer_index + 2]

            x = block([x, w0, w1])
            y = upscale2d(y)
            y = torgb([x, y, w2])

            layer_index += 2

        images_out = y
        return images_out


class Renderer(tf.keras.Model):
    def __init__(self, g_params, **kwargs):
        super(Renderer, self).__init__(**kwargs)

        self.x_dim = g_params['x_dim']
        self.w_dim = g_params['w_dim']
        self.x_depth = g_params['x_depth']
        self.n_mapping = g_params['n_mapping']
        self.resolutions = g_params['resolutions']
        self.featuremaps = g_params['featuremaps']
        self.w_ema_decay = g_params['w_ema_decay']
        self.style_mixing_prob = g_params['style_mixing_prob']
        self.truncation_psi = g_params['truncation_psi']
        self.truncation_cutoff = g_params['truncation_cutoff']

        self.n_broadcast = len(self.resolutions) * 2

        self.mixing_layer_indices = np.arange(self.n_broadcast)[np.newaxis, :, np.newaxis]
        ones = np.ones_like(self.mixing_layer_indices, dtype=np.float32)
        if self.truncation_cutoff is None:
            self.truncation_coefs = ones * self.truncation_psi
        else:
            self.truncation_coefs = ones
            for index in range(self.n_broadcast):
                if index < self.truncation_cutoff:
                    self.truncation_coefs[:, index, :] = self.truncation_psi

        self.g_mapping = Mapping(self.x_dim, self.w_dim, self.n_mapping, self.n_broadcast, name='g_mapping')
        self.synthesis = Synthesis(self.w_dim, self.resolutions, self.featuremaps, name='g_synthesis')

    def build(self, input_shape):
        # w_avg
        self.w_avg = tf.Variable(tf.zeros(shape=[self.w_dim], dtype=tf.dtypes.float32), name='w_avg', trainable=False)

    def set_as_moving_average_of(self, src_net, beta=0.99, beta_nontrainable=0.0):
        def split_first_name(name):
            splitted = name.split('/')
            new_name = '/'.join(splitted[1:])
            return new_name

        for cw in self.trainable_weights:
            cw_name = split_first_name(cw.name)
            for sw in src_net.trainable_weights:
                sw_name = split_first_name(sw.name)
                if cw_name == sw_name:
                    assert sw.shape == cw.shape
                    cw.assign(lerp(sw, cw, beta))
                    break

        for cw in self.non_trainable_weights:
            cw_name = split_first_name(cw.name)
            for sw in src_net.non_trainable_weights:
                sw_name = split_first_name(sw.name)
                if cw_name == sw_name:
                    assert sw.shape == cw.shape
                    cw.assign(lerp(sw, cw, beta_nontrainable))
                    break
        return

    def update_moving_average_of_w(self, w_broadcasted):
        # compute average of current w
        batch_avg = tf.reduce_mean(w_broadcasted[:, 0], axis=0)

        # compute moving average of w and update(assign) w_avg
        self.w_avg.assign(lerp(batch_avg, self.w_avg, self.w_ema_decay))
        return

    def draw_random_x(self, x1):
        batch_size = tf.shape(x1)[0]
        new_x = list()
        for depth in self.x_depth:
            vals = tf.random.uniform(shape=[batch_size], minval=0, maxval=depth, dtype=tf.dtypes.int32)
            onehot = tf.one_hot(vals, depth)
            new_x.append(onehot)
        return tf.concat(new_x, axis=1)

    def style_mixing_regularization(self, x1, w_broadcasted1):
        # get another w and broadcast it
        x2 = self.draw_random_x(x1)
        w_broadcasted2 = self.g_mapping(x2)

        # find mixing limit index
        if tf.random.uniform([], 0.0, 1.0) < self.style_mixing_prob:
            mixing_cutoff_index = tf.random.uniform([], 1, self.n_broadcast, dtype=tf.dtypes.int32)
        else:
            mixing_cutoff_index = tf.constant(self.n_broadcast, dtype=tf.dtypes.int32)

        # mix it
        mixed_w_broadcasted = tf.where(
            condition=tf.broadcast_to(self.mixing_layer_indices < mixing_cutoff_index, tf.shape(w_broadcasted1)),
            x=w_broadcasted1,
            y=w_broadcasted2)
        return mixed_w_broadcasted

    def truncation_trick(self, w_broadcasted):
        truncated_w_broadcasted = lerp(self.w_avg, w_broadcasted, self.truncation_coefs)
        return truncated_w_broadcasted

    def call(self, inputs, training=None, mask=None):
        x = inputs
        w_broadcasted = self.g_mapping(x)

        if training:
            self.update_moving_average_of_w(w_broadcasted)
            w_broadcasted = self.style_mixing_regularization(x, w_broadcasted)

        if not training:
            w_broadcasted = self.truncation_trick(w_broadcasted)

        image_out = self.synthesis(w_broadcasted)
        return image_out


def main():
    batch_size = 4
    g_params_with_label = {
        'x_dim': 250,
        'w_dim': 512,
        'x_depth': [48, 11, 5, 11, 8, 2, 7, 9, 7, 8, 5, 3, 2, 3, 3, 2, 2, 5, 5, 5, 5, 4, 3, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 3, 5, 5, 3, 3, 3, 5, 5, 5],
        'n_mapping': 8,
        'resolutions': [4, 8, 16, 32, 64, 128, 256],
        'featuremaps': [512, 512, 512, 512, 512, 256, 128],
        'w_ema_decay': 0.995,
        'style_mixing_prob': 0.9,
        'truncation_psi': 0.5,
        'truncation_cutoff': None,
    }

    # # new_x = list()
    # # for depth in g_params_with_label['x_depth']:
    # #     val = np.random.randint(0, depth)
    # #     onehot = np.eye(depth, dtype=np.float32)[val]
    # #     new_x.extend(onehot.tolist())
    # new_x = list()
    # for depth in g_params_with_label['x_depth']:
    #     vals = tf.random.uniform(shape=[batch_size], minval=0, maxval=depth, dtype=tf.dtypes.int32)
    #     onehot = tf.one_hot(vals, depth)
    #     new_x.append(onehot)
    # new_x = tf.concat(new_x, axis=1)

    test_x = np.ones((batch_size, g_params_with_label['x_dim']), dtype=np.float32)

    renderer = Renderer(g_params_with_label)
    fake_images1 = renderer(test_x, training=True)
    fake_images2 = renderer(test_x, training=False)
    renderer.summary()

    print(fake_images1.shape)

    print()
    for v in renderer.variables:
        print('{}: {}'.format(v.name, v.shape))
    return


if __name__ == '__main__':
    main()
