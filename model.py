import numpy as np
import tensorflow as tf
import math, csv, time, sys, os, pdb, copy


def get_activation(activation):
    if activation == "softmax":
        output = tf.nn.softmax
    elif activation is None:
        output = None
    elif activation == "tanh":
        output = tf.nn.tanh
    elif activation == "relu":
        output = tf.nn.relu
    elif "leaky_relu" in activation:
        output = lambda x: tf.nn.relu(x, alpha=float(activation.split(" ")[1]))
    elif activation == "linear":
        output = None
    elif activation == "sigmoid":
        output = tf.nn.sigmoid
    else:
        print "activation not recognized:", activation
        raise NotImplementedError

    return output


def get_init(model, t, conv=False):
    initializers = {"zeros": tf.constant_initializer(0.), "norm": tf.random_normal_initializer(0.1)}

    if conv:
        return tf.random_normal_initializer()

    if t not in model:
        if t == "b":
            return tf.constant_initializer(0.)
    
        return tf.random_normal_initializer()

    elif isinstance(model[t], basestring):
        return initializers[model[t]]

    elif isinstance(model[t], int):
        return tf.constant_initializer(model[t])

    else:
        return model[t]


class Model():
    def __call__(self, *args, **kwargs):
        return self.apply(*args, **kwargs)

    def get_activation(self, model):
        activation = model["activation"] if "activation" in model else "linear"
        return get_activation(activation)

    def create_layer(self, inputs, model, dnn_type=True, name=None):
        layer = None
        if model["model_type"] == "conv":
            poolsize = tuple(model["pool"]) if "pool" in model else (1,1)
            stride = tuple(model["stride"]) if "stride" in model else (1,1)

            layer = tf.layers.conv2d(
                inputs=inputs, 
                filters=model["out_size"], 
                kernel_size=model["filter_size"], 
                strides=stride, 
                activation=self.get_activation(model),
                kernel_initializer=get_init(model, "W", conv=True),
                bias_initializer=get_init(model, "b"),
                padding="valid" if "pad" not in model else model["pad"],
                name=model["name"]
            )

        elif model["model_type"] == "flatten":
            return tf.reshape(inputs, [-1, 3136]) # TODO: Use Reshape and Model size

        elif model["model_type"] == "mlp":
            layer = tf.layers.dense(
                inputs=inputs, 
                units=model["out_size"],
                activation=self.get_activation(model),
                kernel_initializer=get_init(model, "W"),
                bias_initializer=get_init(model, "b"),
                name=model["name"]
            )
    
        elif model["model_type"] == "option":
            if name == 'termination_fn':
                layer = tf.layers.dense(
                    inputs=inputs,
                    units=self.num_options,
                    activation=tf.nn.sigmoid,
                    kernel_initializer=get_init(model, "W"),
                    bias_initializer=get_init(model, "b"),
                    name='termination_fn'
                )

            elif name == 'policy_over_options':
                layer = tf.layers.dense(
                    inputs=inputs,
                    units=self.num_options + self.num_actions,
                    activation=tf.nn.softmax,
                    kernel_initializer=get_init(model, "W"),
                    bias_initializer=get_init(model, "b"),
                    name='policy_over_options'
                )

            elif name == 'q_values_options':
                layer = tf.layers.dense(
                    inputs=inputs,
                    units=self.num_options,
                    activation=None,
                    kernel_initializer=get_init(model, "W"),
                    bias_initializer=get_init(model, "b"),
                    name='q_values_options'
                )

            elif name == "value":
                layer = tf.layers.dense(
                    inputs=inputs,
                    units=1,
                    activation=None,
                    kernel_initializer=get_init(model, "W"),
                    bias_initializer=get_init(model, "b"),
                    name='value'
                )

            else:
                layer = tf.layers.dense(
                    inputs=inputs,
                    units=self.num_actions,
                    activation=None,
                    kernel_initializer=get_init(model, "W"),
                    bias_initializer=get_init(model, "b"),
                    name=name
                )

        else:
            print "UNKNOWN LAYER NAME"
            raise NotImplementedError

        print(model["model_type"], "is done")
        return layer

    def __init__(self, model_in, input_size=None, rng=1234, dnn_type=False, num_options=4, num_actions=3, scope='global'):
        """
        example model:
        model = [{"model_type": "conv", "filter_size": [5,5], "pool": [1,1], "stride": [1,1], "out_size": 5},
                 {"model_type": "conv", "filter_size": [7,7], "pool": [1,1], "stride": [1,1], "out_size": 15},
                 {"model_type": "mlp", "out_size": 300, "activation": "tanh"},
                 {"model_type": "mlp", "out_size": 10, "activation": "softmax"}]
        """

        tf.set_random_seed(rng)
        self.num_options = num_options
        self.num_actions = num_actions
        self.observations = tf.placeholder(shape=[None, 84, 84, 4], dtype=tf.float32)

        input_tensor = self.X

        print("Building following model...")
        print(model)

        self.model = model
        self.input_size = input_size
        self.out_size = model_in[-2]["out_size"]
        self.dnn_type = dnn_type

        # Build Nain NN
        for i, m in enumerate(model):
            if m["model_type"] == 'option' or m["model_type"] == 'value':
                break

            new_layer = self.create_layer(input_tensor, m, dnn_type=dnn_type)
            input_tensor = new_layer

        self.state_representation = input_tensor


        m = dict()
        m["model_type"] = 'option'
        
        # Build Option Related End Networks 
        self.termination_fn = self.create_layer(input_tensor, m, dnn_type=dnn_type, name='termination_fn')
        self.policy_over_options = self.create_layer(input_tensor, m, dnn_type, name='policy_over_options')
        self.q_values_options = self.create_layer(input_tensor, m, dnn_type, name='q_values_options')

        self.intra_options_q_vals = list()
        for i in range(self.num_options):
            intra_option = self.create_layer(input_tensor, m, dnn_type=dnn_type, name='intra_option_{}'.format(i))
            self.intra_options_q_vals.append(intra_option)

        m = dict()
        m["model_type"] = 'value'

        self.value_fn = self.create_layer(input_tensor, m, dnn_type=dnn_type, name='value')

        print("Build complete.")

        if scope != 'global':
            print("Building worker specific operations.")

            self.actions = tf.placeholder(shape=[None], dtype=tf.float32)
            self.actions_onehot = tf.onehot(self.actions, self.num_actions, dtype=tf.float32)

            self.options = tf.placeholder(shape=[None], dtype=tf.float32)
            self.options_onehot = tf.onehot(self.options, self.num_options, dtype=tf.float32)

            self.targets = tf.placeholder(shape=[None], dtype=tf.float32)
            self.advantages = tf.placeholder(shape=[None], dtype=tf.float32)

            self.index_of_option = tf.argmax(options_onehot)
            self.responsible_actions = tf.reduce_sum(self.actions_onehot * self.intra_options_q_vals[index_of_option])
            self.responsible_options = tf.reduce_sum(self.options_onehot * self.policy_over_options)
            self.responsible_termination = tf.reduce_sum(self.options_onehot * self.termination_fn)

            # Loss functions -- TODO
            self.value_loss = 0.5 * tf.reduce_sum(tf.square(self.targets - tf.reshape(self.value_fn, [-1])))
            self.termination_gradient = 0.0 # TODO
            self.entropy = -1 * tf.reduce_sum(self.actions*tf.log(self.actions + log_eps)) * args.entropy_reg
            self.policy_loss = -1 * tf.reduce_sum(tf.log(self.responsile_actions)*self.advantages)
            self.loss = self.policy_loss + self.entropy - self.value_loss - self.termination_gradient

            local_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope)
            self.gradients = tf.gradients(self.loss, local_vars)
            self.var_norms = tf.global_norm(local_vars)

            grads, self.grad_norms = tf.clip_by_global_norm(self.gradients, 40.0)
            global_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, 'global')
            self.apply_grads = trainer.apply_gradients(zip(grads,global_vars))

    def save_params(self):
        return [i.get_value() for i in self.params]

    def load_params(self, values):
        print("LOADING NNET..")

        for p, value in zip(self.params, values):
            p.set_value(value.astype("float32"))

        print("LOADED")

if __name__ == '__main__':
    model = [
        {"model_type": "conv", "filter_size": [8,8], "pool": [1,1], "stride": [4,4], "out_size": 32, "name": "conv1"},
        {"model_type": "conv", "filter_size": [4,4], "pool": [1,1], "stride": [2,2], "out_size": 64, "name": "conv2"},
        {"model_type": "conv", "filter_size": [3,3], "pool": [1,1], "stride": [1,1], "out_size": 64, "name": "conv3"},
        {"model_type": "flatten"},
        {"model_type": "mlp", "out_size": 512, "activation": "sigmoid", "name": "fc1"},
        {"model_type": "option"},
        {"model_type": "value"}
    ]

    m = Model(model)

    with tf.Session() as sess:
        init_op = tf.global_variables_initializer()
        l_init_op = tf.local_variables_initializer()

        writer = tf.summary.FileWriter('log', sess.graph)

        pseudo_input = np.zeros([1, 84, 84, 4])
        feed_dict = {m.X : pseudo_input}

        sess.run(init_op)
        sess.run(l_init_op)

        init_ops = [m.termination_fn, m.policy_over_options] + m.intra_options
        summary = sess.run(init_ops, feed_dict=feed_dict)
        # writer.add_summary(summary)