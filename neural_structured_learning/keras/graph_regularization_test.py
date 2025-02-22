# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for neural_structured_learning.keras.graph_regularization.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

from absl.testing import parameterized
import neural_structured_learning.configs as configs
from neural_structured_learning.keras import graph_regularization
import numpy as np
import tensorflow as tf

from google.protobuf import text_format
from tensorflow.python.framework import test_util  # pylint: disable=g-direct-tensorflow-import

FEATURE_NAME = 'x'
LABEL_NAME = 'y'
NBR_FEATURE_PREFIX = 'NL_nbr_'
NBR_WEIGHT_SUFFIX = '_weight'
LEARNING_RATE = 0.01


def make_feature_spec(input_shape, max_neighbors):
  """Returns a feature spec that can be used to parse tf.train.Examples.

  Args:
    input_shape: A list of integers representing the shape of the input feature
      and corresponding neighbor features.
    max_neighbors: The maximum neighbors per sample to be used for graph
      regularization.
  """
  feature_spec = {
      FEATURE_NAME:
          tf.io.FixedLenFeature(input_shape, tf.float32),
      LABEL_NAME:
          tf.io.FixedLenFeature([1],
                                tf.float32,
                                default_value=tf.constant([0.0])),
  }
  for i in range(max_neighbors):
    nbr_feature_key = '{}{}_{}'.format(NBR_FEATURE_PREFIX, i, FEATURE_NAME)
    nbr_weight_key = '{}{}{}'.format(NBR_FEATURE_PREFIX, i, NBR_WEIGHT_SUFFIX)
    feature_spec[nbr_feature_key] = tf.io.FixedLenFeature(
        input_shape, tf.float32)
    feature_spec[nbr_weight_key] = tf.io.FixedLenFeature(
        [1], tf.float32, default_value=tf.constant([0.0]))
  return feature_spec


def build_linear_sequential_model(input_shape, weights, num_output=1):
  model = tf.keras.Sequential()
  model.add(
      tf.keras.layers.InputLayer(input_shape=input_shape, name=FEATURE_NAME))
  model.add(
      tf.keras.layers.Dense(
          num_output,
          input_shape=input_shape,
          use_bias=False,
          name='dense',
          kernel_initializer=tf.keras.initializers.Constant(weights)))
  return model


def build_linear_functional_model(input_shape, weights, num_output=1):
  inputs = tf.keras.Input(shape=input_shape, name=FEATURE_NAME)
  outputs = tf.keras.layers.Dense(
      num_output,
      use_bias=False,
      kernel_initializer=tf.keras.initializers.Constant(weights))(
          inputs)
  return tf.keras.Model(inputs=inputs, outputs=outputs)


def build_linear_subclass_model(input_shape, weights, num_output=1):
  del input_shape

  class CustomLinearModel(tf.keras.Model):

    def __init__(self, weights, num_output, name=None):
      super(CustomLinearModel, self).__init__(name=name)
      self.init_weights = weights
      self.num_output = num_output
      self.dense = tf.keras.layers.Dense(
          num_output,
          use_bias=False,
          name='dense',
          kernel_initializer=tf.keras.initializers.Constant(weights))

    def call(self, inputs):
      return self.dense(inputs[FEATURE_NAME])

    def get_config(self):
      return {
          'name': self.name,
          'weights': self.init_weights,
          'num_output': self.num_output
      }

  return CustomLinearModel(weights, num_output)


def make_dataset(example_proto, input_shape, training, max_neighbors):
  """Construct a tf.data.Dataset from the given Example."""

  def make_parse_example_fn(feature_spec):

    def parse_example(serialized_example_proto):
      """Extracts relevant fields from the example_proto."""
      feature_dict = tf.io.parse_single_example(serialized_example_proto,
                                                feature_spec)
      return feature_dict, feature_dict.pop(LABEL_NAME)

    return parse_example

  example = text_format.Parse(example_proto, tf.train.Example())
  serialized_example = example.SerializeToString()
  dataset = tf.data.Dataset.from_tensors(
      tf.convert_to_tensor(serialized_example))
  if training:
    dataset = dataset.shuffle(10)
  dataset = dataset.map(
      make_parse_example_fn(make_feature_spec(input_shape, max_neighbors)))
  dataset = dataset.batch(1)
  return dataset


class GraphRegularizationTest(tf.test.TestCase, parameterized.TestCase):

  def test_predict_regularized_model(self):
    model = build_linear_functional_model(
        input_shape=(2,), weights=np.array([1.0, -1.0]))
    inputs = {FEATURE_NAME: tf.constant([[5.0, 3.0]])}

    graph_reg_model = graph_regularization.GraphRegularization(model)
    graph_reg_model.compile(optimizer=tf.keras.optimizers.SGD(0.01), loss='MSE')

    prediction = graph_reg_model.predict(x=inputs, steps=1, batch_size=1)

    self.assertAllEqual([[1 * 5.0 + (-1.0) * 3.0]], prediction)

  def test_predict_base_model(self):
    model = build_linear_functional_model(
        input_shape=(2,), weights=np.array([1.0, -1.0]))
    inputs = {FEATURE_NAME: tf.constant([[5.0, 3.0]])}

    graph_reg_model = graph_regularization.GraphRegularization(model)
    graph_reg_model.compile(optimizer=tf.keras.optimizers.SGD(0.01), loss='MSE')

    prediction = model.predict(x=inputs, steps=1, batch_size=1)

    self.assertAllEqual([[1 * 5.0 + (-1.0) * 3.0]], prediction)

  def _train_and_check_params(self,
                              example,
                              model_fn,
                              dense_layer_index,
                              max_neighbors,
                              weight,
                              expected_grad_from_weight,
                              distributed_strategy=None):
    """Runs training for one step and verifies gradient-based updates.

    This uses a linear regressor as the base model.

    Args:
      example: An instance of `tf.train.Example`.
      model_fn: A function that builds a linear regression model.
      dense_layer_index: The index of the dense layer in the linear regressor.
      max_neighbors: The maximum number of neighbors for graph regularization.
      weight: Initial value for the weights variable in the linear regressor.
      expected_grad_from_weight: The expected gradient of the loss with
        respect to the weights variable.
      distributed_strategy: An instance of `tf.distribute.Strategy` specifying
        the distributed strategy to use for training.
    """

    dataset = make_dataset(
        example, input_shape=[2], training=True, max_neighbors=max_neighbors)

    def _create_and_compile_graph_reg_model(model_fn, weight, max_neighbors):
      """Creates and compiles a graph regularized model.

      Args:
        model_fn: A function that builds a linear regression model.
        weight: Initial value for the weights variable in the linear regressor.
        max_neighbors: The maximum number of neighbors for graph regularization.

      Returns:
        A pair containing the unregularized model and the graph regularized
        model as `tf.keras.Model` instances.
      """
      model = model_fn((2,), weight)
      graph_reg_config = configs.make_graph_reg_config(
          max_neighbors=max_neighbors, multiplier=1)
      graph_reg_model = graph_regularization.GraphRegularization(
          model, graph_reg_config)
      graph_reg_model.compile(
          optimizer=tf.keras.optimizers.SGD(LEARNING_RATE), loss='MSE')
      return model, graph_reg_model

    if distributed_strategy:
      with distributed_strategy.scope():
        model, graph_reg_model = _create_and_compile_graph_reg_model(
            model_fn, weight, max_neighbors)
    else:
      model, graph_reg_model = _create_and_compile_graph_reg_model(
          model_fn, weight, max_neighbors)

    graph_reg_model.fit(x=dataset, epochs=1, steps_per_epoch=1)

    # Compute the new weight value based on the gradient.
    expected_weight = weight - LEARNING_RATE * (expected_grad_from_weight)

    # Check that the weight parameter of the linear regressor has the correct
    # value.
    self.assertAllClose(expected_weight,
                        model.layers[dense_layer_index].weights[0].value())

  @parameterized.named_parameters([
      ('_sequential', 0, build_linear_sequential_model),
      ('_functional', 1, build_linear_functional_model),
      ('_subclass', 0, build_linear_subclass_model),
  ])
  @test_util.run_in_graph_and_eager_modes
  def test_graph_reg_model_one_neighbor_training(self, dense_layer_index,
                                                 model_fn):
    w = np.array([[4.0], [-3.0]])
    x0 = np.array([[2.0, 3.0]])
    x0_nbr0 = np.array([[2.5, 3.0]])
    y0 = np.array([[0.0]])

    example = """
                features {
                  feature {
                    key: "x"
                    value: { float_list { value: [ 2.0, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_x"
                    value: { float_list { value: [ 2.5, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_weight"
                    value: { float_list { value: 1.0 } }
                  }
                  feature {
                    key: "y"
                    value: { float_list { value: 0.0 } }
                  }
                }
              """

    y_hat = np.dot(x0, w)  # -1.0
    y_nbr = np.dot(x0_nbr0, w)  # 1.0

    # The graph loss term is (y_hat - y_nbr)^2 since graph regularization is
    # done on the final predictions.
    grad_w = 2 * (y_hat - y0) * x0.T + 2 * (y_hat - y_nbr) * (
        x0 - x0_nbr0).T  # [[-2.0], [-6.0]]

    self._train_and_check_params(
        example,
        model_fn,
        dense_layer_index,
        max_neighbors=1,
        weight=w,
        expected_grad_from_weight=grad_w)

  def _test_training_with_two_neighbors(self,
                                        dense_layer_index,
                                        model_fn,
                                        distributed_strategy=None):
    w = np.array([[4.0], [-3.0]])
    x0 = np.array([[2.0, 3.0]])
    x0_nbr0 = np.array([[2.5, 3.0]])
    x0_nbr1 = np.array([[2.0, 2.0]])
    y0 = np.array([[0.0]])

    example = """
                features {
                  feature {
                    key: "x"
                    value: { float_list { value: [ 2.0, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_x"
                    value: { float_list { value: [ 2.5, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_weight"
                    value: { float_list { value: 1.0 } }
                  }
                  feature {
                    key: "NL_nbr_1_x"
                    value: { float_list { value: [ 2.0, 2.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_1_weight"
                    value: { float_list { value: 1.0 } }
                  }
                  feature {
                    key: "y"
                    value: { float_list { value: 0.0 } }
                  }
                }
              """

    y_hat = np.dot(x0, w)  # -1.0
    y_nbr0 = np.dot(x0_nbr0, w)  # 1.0
    y_nbr1 = np.dot(x0_nbr1, w)  # 2.0

    # The distance metric for the graph loss is 'L2'. So, the graph loss term is
    # [(y_hat - y_nbr_0)^2 + (y_hat - y_nbr_1)^2] / 2
    grad_w = 2 * (y_hat - y0) * x0.T + (y_hat - y_nbr0) * (x0 - x0_nbr0).T + (
        y_hat - y_nbr1) * (x0 - x0_nbr1).T  # [[-3.0], [-9.0]]

    self._train_and_check_params(
        example,
        model_fn,
        dense_layer_index,
        max_neighbors=2,
        weight=w,
        expected_grad_from_weight=grad_w,
        distributed_strategy=distributed_strategy)

  @parameterized.named_parameters([
      ('_sequential', 0, build_linear_sequential_model),
      ('_functional', 1, build_linear_functional_model),
      ('_subclass', 0, build_linear_subclass_model),
  ])
  @test_util.run_in_graph_and_eager_modes
  def test_graph_reg_model_two_neighbors_training(self, dense_layer_index,
                                                  model_fn):
    self._test_training_with_two_neighbors(dense_layer_index, model_fn)

  @parameterized.named_parameters([
      ('_sequential', 0, build_linear_sequential_model),
      ('_functional', 1, build_linear_functional_model),
      ('_subclass', 0, build_linear_subclass_model),
  ])
  @test_util.run_v2_only
  def test_graph_reg_model_distributed_strategy(self, dense_layer_index,
                                                model_fn):
    self._test_training_with_two_neighbors(
        dense_layer_index,
        model_fn,
        distributed_strategy=tf.distribute.MirroredStrategy())

  def _train_and_check_eval_results(self,
                                    train_example,
                                    test_example,
                                    model_fn,
                                    max_neighbors,
                                    weight,
                                    distributed_strategy=None):
    """Verifies eval results for the graph-regularized model.

    This uses a linear regressor as the base model.

    Args:
      train_example: An instance of `tf.train.Example` used for training.
      test_example: An instance of `tf.train.Example` used for evaluation.
      model_fn: A function that builds a linear regression model.
      max_neighbors: The maximum number of neighbors for graph regularization.
      weight: Initial value for the weights variable in the linear regressor.
      distributed_strategy: An instance of `tf.distribute.Strategy` specifying
        the distributed strategy to use for training.
    """

    train_dataset = make_dataset(
        train_example,
        input_shape=[2],
        training=True,
        max_neighbors=max_neighbors)

    test_dataset = make_dataset(
        test_example, input_shape=[2], training=False, max_neighbors=0)

    def _create_and_compile_graph_reg_model(model_fn, weight, max_neighbors):
      """Creates and compiles a graph regularized model.

      Args:
        model_fn: A function that builds a linear regression model.
        weight: Initial value for the weights variable in the linear regressor.
        max_neighbors: The maximum number of neighbors for graph regularization.

      Returns:
        A pair containing the unregularized model and the graph regularized
        model as `tf.keras.Model` instances.
      """
      model = model_fn((2,), weight)
      graph_reg_config = configs.make_graph_reg_config(
          max_neighbors=max_neighbors, multiplier=1)
      graph_reg_model = graph_regularization.GraphRegularization(
          model, graph_reg_config)
      graph_reg_model.compile(
          optimizer=tf.keras.optimizers.SGD(LEARNING_RATE),
          loss='MSE',
          metrics=['accuracy'])
      return model, graph_reg_model

    if distributed_strategy:
      with distributed_strategy.scope():
        model, graph_reg_model = _create_and_compile_graph_reg_model(
            model_fn, weight, max_neighbors)
    else:
      model, graph_reg_model = _create_and_compile_graph_reg_model(
          model_fn, weight, max_neighbors)

    graph_reg_model.fit(x=train_dataset, epochs=1, steps_per_epoch=1)

    # Evaluating the graph-regularized model should yield the same results
    # as evaluating the base model as the former involves just using the
    # base model for evaluation.
    graph_reg_model_eval_results = dict(
        zip(graph_reg_model.metrics_names,
            graph_reg_model.evaluate(x=test_dataset)))
    base_model_eval_results = dict(
        zip(model.metrics_names, model.evaluate(x=test_dataset)))
    self.assertAllClose(base_model_eval_results, graph_reg_model_eval_results)

  @parameterized.named_parameters([
      ('_sequential', build_linear_sequential_model),
      ('_functional', build_linear_functional_model),
      ('_subclass', build_linear_subclass_model),
  ])
  def test_graph_reg_model_evaluate(self, model_fn):
    w = np.array([[4.0], [-3.0]])

    train_example = """
                features {
                  feature {
                    key: "x"
                    value: { float_list { value: [ 2.0, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_x"
                    value: { float_list { value: [ 2.5, 3.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_0_weight"
                    value: { float_list { value: 1.0 } }
                  }
                  feature {
                    key: "NL_nbr_1_x"
                    value: { float_list { value: [ 2.0, 2.0 ] } }
                  }
                  feature {
                    key: "NL_nbr_1_weight"
                    value: { float_list { value: 1.0 } }
                  }
                  feature {
                    key: "y"
                    value: { float_list { value: 0.0 } }
                  }
                }
              """

    test_example = """
                features {
                  feature {
                    key: "x"
                    value: { float_list { value: [ 4.0, 2.0 ] } }
                  }
                  feature {
                    key: "y"
                    value: { float_list { value: 4.0 } }
                  }
                }
              """
    self._train_and_check_eval_results(
        train_example,
        test_example,
        model_fn,
        max_neighbors=2,
        weight=w,
        distributed_strategy=None)

  def _test_graph_reg_model_save(self, model_fn):
    """Template for testing model saving and loading."""
    w = np.array([[4.0], [-3.0]])
    base_model = model_fn((2,), w)
    graph_reg_config = configs.make_graph_reg_config(
        max_neighbors=1, multiplier=1)
    graph_reg_model = graph_regularization.GraphRegularization(
        base_model, graph_reg_config)
    graph_reg_model.compile(
        optimizer=tf.keras.optimizers.SGD(LEARNING_RATE),
        loss='MSE',
        metrics=['accuracy'])

    # Run the model before saving it. This is necessary for subclassed models.
    inputs = {FEATURE_NAME: tf.constant([[5.0, 3.0]])}
    graph_reg_model.predict(inputs, steps=1, batch_size=1)
    saved_model_dir = os.path.join(self.get_temp_dir(), 'saved_model')
    graph_reg_model.save(saved_model_dir)

    loaded_model = tf.keras.models.load_model(saved_model_dir)
    self.assertEqual(
        len(loaded_model.trainable_weights),
        len(graph_reg_model.trainable_weights))
    for w_loaded, w_graph_reg in zip(loaded_model.trainable_weights,
                                     graph_reg_model.trainable_weights):
      self.assertAllClose(
          tf.keras.backend.get_value(w_loaded),
          tf.keras.backend.get_value(w_graph_reg))

  @parameterized.named_parameters([
      ('_sequential', build_linear_sequential_model),
      ('_functional', build_linear_functional_model),
  ])
  def test_graph_reg_model_save(self, model_fn):
    self._test_graph_reg_model_save(model_fn)

  # Saving subclassed models are only supported in TF v2.
  @test_util.run_v2_only
  def test_graph_reg_model_save_subclass(self):
    self._test_graph_reg_model_save(build_linear_subclass_model)


if __name__ == '__main__':
  tf.test.main()
