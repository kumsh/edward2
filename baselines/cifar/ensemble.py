# coding=utf-8
# Copyright 2020 The Edward2 Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Ensemble on CIFAR.

This script only performs evaluation, not training. We recommend training
ensembles by launching independent runs of `deterministic.py` over different
seeds.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import os

from absl import app
from absl import flags
from absl import logging

import edward2 as ed
import deterministic  # local file import
import utils  # local file import
import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_datasets as tfds

# TODO(trandustin): We inherit
# FLAGS.{dataset,per_core_batch_size,output_dir,seed} from deterministic. This
# is not intuitive, which suggests we need to either refactor to avoid importing
# from a binary or duplicate the model definition here.
flags.DEFINE_string('checkpoint_dir', None,
                    'The directory where the model weights are stored.')
flags.mark_flag_as_required('checkpoint_dir')
FLAGS = flags.FLAGS


def ensemble_negative_log_likelihood(labels, logits):
  """Negative log-likelihood for ensemble.

  For each datapoint (x,y), the ensemble's negative log-likelihood is:

  ```
  -log p(y|x) = -log sum_{m=1}^{ensemble_size} exp(log p(y|x,theta_m)) +
                log ensemble_size.
  ```

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  ensemble_size = float(logits.shape[0])
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...], tf.shape(logits)[:-1]),
      logits)
  return -tf.reduce_logsumexp(-nll, axis=0) + tf.math.log(ensemble_size)


def gibbs_cross_entropy(labels, logits):
  """Average cross entropy for ensemble members (Gibbs cross entropy).

  For each datapoint (x,y), the ensemble's Gibbs cross entropy is:

  ```
  GCE = - (1/ensemble_size) sum_{m=1}^ensemble_size log p(y|x,theta_m).
  ```

  The Gibbs cross entropy approximates the average cross entropy of a single
  model drawn from the (Gibbs) ensemble.

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...], tf.shape(logits)[:-1]),
      logits)
  return tf.reduce_mean(nll, axis=0)


def main(argv):
  del argv  # unused arg
  if not FLAGS.use_gpu:
    raise ValueError('Only GPU is currently supported.')
  if FLAGS.num_cores > 1:
    raise ValueError('Only a single accelerator is currently supported.')
  tf.enable_v2_behavior()
  tf.random.set_seed(FLAGS.seed)
  tf.io.gfile.makedirs(FLAGS.output_dir)

  ds_info = tfds.builder(FLAGS.dataset).info
  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores
  steps_per_eval = ds_info.splits['test'].num_examples // batch_size
  num_classes = ds_info.features['label'].num_classes

  dataset_input_fn = utils.load_input_fn(
      split=tfds.Split.TEST,
      name=FLAGS.dataset,
      batch_size=FLAGS.per_core_batch_size,
      use_bfloat16=FLAGS.use_bfloat16)
  test_datasets = {'clean': dataset_input_fn()}
  corruption_types, max_intensity = utils.load_corrupted_test_info(
      FLAGS.dataset)
  for name in corruption_types:
    for intensity in range(1, max_intensity + 1):
      dataset_name = '{0}_{1}'.format(name, intensity)
      if FLAGS.dataset == 'cifar10':
        load_c_dataset = utils.load_cifar10_c_input_fn
      else:
        load_c_dataset = functools.partial(
            utils.load_cifar100_c_input_fn, path=FLAGS.cifar100_c_path)
      corrupted_input_fn = load_c_dataset(
          corruption_name=name,
          corruption_intensity=intensity,
          batch_size=FLAGS.per_core_batch_size,
          use_bfloat16=FLAGS.use_bfloat16)
      test_datasets[dataset_name] = corrupted_input_fn()

  model = deterministic.wide_resnet(
      input_shape=ds_info.features['image'].shape,
      depth=28,
      width_multiplier=10,
      num_classes=num_classes,
      l2=0.,
      version=2)
  logging.info('Model input shape: %s', model.input_shape)
  logging.info('Model output shape: %s', model.output_shape)
  logging.info('Model number of weights: %s', model.count_params())

  # Search for checkpoints from their index file; then remove the index suffix.
  ensemble_filenames = tf.io.gfile.glob(os.path.join(FLAGS.checkpoint_dir,
                                                     '**/*.index'))
  ensemble_filenames = [filename[:-6] for filename in ensemble_filenames]
  ensemble_size = len(ensemble_filenames)
  logging.info('Ensemble size: %s', ensemble_size)
  logging.info('Ensemble number of weights: %s',
               ensemble_size * model.count_params())
  logging.info('Ensemble filenames: %s', str(ensemble_filenames))
  checkpoint = tf.train.Checkpoint(model=model)

  # Write model predictions to files.
  num_datasets = len(test_datasets)
  for m, ensemble_filename in enumerate(ensemble_filenames):
    checkpoint.restore(ensemble_filename)
    for n, (name, test_dataset) in enumerate(test_datasets.items()):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      if not tf.io.gfile.exists(filename):
        logits = []
        test_iterator = iter(test_dataset)
        for _ in range(steps_per_eval):
          features, _ = next(test_iterator)
          logits.append(model(features, training=False))

        logits = tf.concat(logits, axis=0)
        with tf.io.gfile.GFile(filename, 'w') as f:
          np.save(f, logits.numpy())
      percent = (m * num_datasets + (n + 1)) / (ensemble_size * num_datasets)
      message = ('{:.1%} completion for prediction: ensemble member {:d}/{:d}. '
                 'Dataset {:d}/{:d}'.format(percent,
                                            m + 1,
                                            ensemble_size,
                                            n + 1,
                                            num_datasets))
      logging.info(message)

  metrics = {
      'test/negative_log_likelihood': tf.keras.metrics.Mean(),
      'test/gibbs_cross_entropy': tf.keras.metrics.Mean(),
      'test/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
      'test/ece': ed.metrics.ExpectedCalibrationError(num_classes=num_classes,
                                                      num_bins=FLAGS.num_bins),
  }
  corrupt_metrics = {}
  for name in test_datasets:
    corrupt_metrics['test/nll_{}'.format(name)] = tf.keras.metrics.Mean()
    corrupt_metrics['test/accuracy_{}'.format(name)] = (
        tf.keras.metrics.SparseCategoricalAccuracy())
    corrupt_metrics['test/ece_{}'.format(
        name)] = ed.metrics.ExpectedCalibrationError(
            num_classes=num_classes, num_bins=FLAGS.num_bins)

  # Evaluate model predictions.
  for n, (name, test_dataset) in enumerate(test_datasets.items()):
    logits_dataset = []
    for m in range(ensemble_size):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      with tf.io.gfile.GFile(filename, 'rb') as f:
        logits_dataset.append(np.load(f))

    logits_dataset = tf.convert_to_tensor(logits_dataset)
    test_iterator = iter(test_dataset)
    for step in range(steps_per_eval):
      _, labels = next(test_iterator)
      logits = logits_dataset[:, (step*batch_size):((step+1)*batch_size)]
      labels = tf.cast(labels, tf.int32)
      negative_log_likelihood = tf.reduce_mean(
          ensemble_negative_log_likelihood(labels, logits))
      per_probs = tf.nn.softmax(logits)
      probs = tf.reduce_mean(per_probs, axis=0)
      if name == 'clean':
        gibbs_ce = tf.reduce_mean(gibbs_cross_entropy(labels, logits))
        metrics['test/negative_log_likelihood'].update_state(
            negative_log_likelihood)
        metrics['test/gibbs_cross_entropy'].update_state(gibbs_ce)
        metrics['test/accuracy'].update_state(labels, probs)
        metrics['test/ece'].update_state(labels, probs)
      else:
        corrupt_metrics['test/nll_{}'.format(name)].update_state(
            negative_log_likelihood)
        corrupt_metrics['test/accuracy_{}'.format(name)].update_state(
            labels, probs)
        corrupt_metrics['test/ece_{}'.format(name)].update_state(
            labels, probs)

    message = ('{:.1%} completion for evaluation: dataset {:d}/{:d}'.format(
        (n + 1) / num_datasets, n + 1, num_datasets))
    logging.info(message)

  corrupt_results = utils.aggregate_corrupt_metrics(corrupt_metrics,
                                                    corruption_types,
                                                    max_intensity)
  total_results = {name: metric.result() for name, metric in metrics.items()}
  total_results.update(corrupt_results)
  logging.info('Metrics: %s', total_results)


if __name__ == '__main__':
  app.run(main)
