import collections
import itertools
from functools import partial

import jax
import numpy as np
import tree

from keras import backend
from keras import callbacks as callbacks_module
from keras import ops
from keras import optimizers as optimizers_module
from keras.backend import distribution_lib as jax_distribution_lib
from keras.distribution import distribution_lib
from keras.trainers import trainer as base_trainer
from keras.trainers.data_adapters import data_adapter_utils
from keras.trainers.epoch_iterator import EpochIterator
from keras.utils import traceback_utils


class JAXTrainer(base_trainer.Trainer):
    def __init__(self):
        super().__init__()
        self.train_function = None
        self.test_function = None
        self.predict_function = None

    def compute_loss_and_updates(
        self,
        trainable_variables,
        non_trainable_variables,
        x,
        y,
        sample_weight,
        training=False,
        optimizer_variables=None,
    ):
        """This method is stateless and is intended for use with jax.grad."""
        kwargs = {}
        if self._call_has_training_arg:
            kwargs["training"] = training
        y_pred, non_trainable_variables, losses = self.stateless_call(
            trainable_variables,
            non_trainable_variables,
            x,
            return_losses=True,
            **kwargs,
        )

        trainable_mapping = zip(self.trainable_variables, trainable_variables)
        with backend.StatelessScope(state_mapping=trainable_mapping):
            # Note that this is needed for the regularization loss, which need
            # the latest value of train/non-trainable variables.
            loss = self.compute_loss(
                x, y, y_pred, sample_weight, allow_empty=True
            )
        if losses:
            loss += ops.sum(losses)
        unscaled_loss = loss
        if training and self.optimizer is not None:
            # Scale loss with a StatelessScope, to use an update scale variable.
            mapping = list(zip(self.optimizer.variables, optimizer_variables))
            with backend.StatelessScope(state_mapping=mapping):
                loss = self.optimizer.scale_loss(loss)
        return loss, (unscaled_loss, y_pred, non_trainable_variables)

    def train_step(self, state, data):
        (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
            metrics_variables,
        ) = state
        x, y, sample_weight = data_adapter_utils.unpack_x_y_sample_weight(data)
        grad_fn = jax.value_and_grad(
            self.compute_loss_and_updates, has_aux=True
        )
        (loss, aux), grads = grad_fn(
            trainable_variables,
            non_trainable_variables,
            x,
            y,
            sample_weight,
            training=True,
            optimizer_variables=optimizer_variables,
        )
        (unscaled_loss, y_pred, non_trainable_variables) = aux

        (
            trainable_variables,
            optimizer_variables,
        ) = self.optimizer.stateless_apply(
            optimizer_variables, grads, trainable_variables
        )

        with backend.StatelessScope(
            state_mapping=[
                (ref_v, v)
                for ref_v, v in zip(self.metrics_variables, metrics_variables)
            ]
        ) as scope:
            self._loss_tracker.update_state(unscaled_loss)
            logs = self.compute_metrics(x, y, y_pred, sample_weight)

        new_metrics_variables = []
        for ref_v in self.metrics_variables:
            new_v = scope.get_current_value(ref_v)
            if new_v is None:
                new_v = ref_v.value
            new_metrics_variables.append(new_v)
        metrics_variables = new_metrics_variables

        state = self._enforce_jax_state_sharding(
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
            metrics_variables,
        )
        return logs, state

    def test_step(self, state, data):
        (
            trainable_variables,
            non_trainable_variables,
            metrics_variables,
        ) = state
        x, y, sample_weight = data_adapter_utils.unpack_x_y_sample_weight(data)
        loss, aux = self.compute_loss_and_updates(
            trainable_variables,
            non_trainable_variables,
            x,
            y,
            sample_weight,
            training=False,
        )
        (unscaled_loss, y_pred, non_trainable_variables) = aux

        with backend.StatelessScope(
            state_mapping=[
                (ref_v, v)
                for ref_v, v in zip(self.metrics_variables, metrics_variables)
            ]
        ) as scope:
            self._loss_tracker.update_state(unscaled_loss)
            logs = self.compute_metrics(x, y, y_pred, sample_weight)

        new_metrics_variables = []
        for ref_v in self.metrics_variables:
            new_v = scope.get_current_value(ref_v)
            if new_v is None:
                new_v = ref_v.value
            new_metrics_variables.append(new_v)
        metrics_variables = new_metrics_variables

        (
            trainable_variables,
            non_trainable_variables,
            _,
            metrics_variables,
        ) = self._enforce_jax_state_sharding(
            trainable_variables=trainable_variables,
            non_trainable_variables=non_trainable_variables,
            optimizer_variables=None,
            metrics_variables=metrics_variables,
        )
        state = (
            trainable_variables,
            non_trainable_variables,
            metrics_variables,
        )
        return logs, state

    def predict_step(self, state, data):
        trainable_variables, non_trainable_variables = state
        kwargs = {}
        if self._call_has_training_arg:
            kwargs["training"] = False

        x, _, _ = data_adapter_utils.unpack_x_y_sample_weight(data)
        outputs, non_trainable_variables = self.stateless_call(
            trainable_variables, non_trainable_variables, x, **kwargs
        )
        (
            trainable_variables,
            non_trainable_variables,
            _,
            _,
        ) = self._enforce_jax_state_sharding(
            trainable_variables=trainable_variables,
            non_trainable_variables=non_trainable_variables,
            optimizer_variables=None,
            metrics_variables=None,
        )
        return outputs, (trainable_variables, non_trainable_variables)

    def make_train_function(self, force=False):
        if self.train_function is not None and not force:
            return self.train_function

        def one_train_step(state, data):
            data = data[0]
            return self.train_step(state, data)

        def multi_train_steps(state, data):
            for single_step_data in data:
                logs, state = one_train_step(state, [single_step_data])
            return logs, state

        if self.steps_per_execution > 1:
            train_step = multi_train_steps
        else:
            train_step = one_train_step

        if not self.run_eagerly and self.jit_compile:
            # Note that we mark the state and data to be donated to jax,
            # so that jax will reuse the memory buffer for outputs.
            # This will reduce the memory usage of the training function by
            # half.
            @partial(jax.jit, donate_argnames="state")
            def compiled_train_step(state, data):
                return train_step(state, data)

            self.train_function = compiled_train_step

        else:
            self.train_function = train_step

    def make_test_function(self, force=False):
        if self.test_function is not None and not force:
            return self.test_function

        def one_test_step(state, data):
            data = data[0]
            return self.test_step(state, data)

        def multi_test_steps(state, data):
            for single_step_data in data:
                logs, state = one_test_step(state, [single_step_data])
            return logs, state

        if self.steps_per_execution > 1:
            test_step = multi_test_steps
        else:
            test_step = one_test_step

        if not self.run_eagerly and self.jit_compile:
            # Note that we mark the state and data to be donated to jax,
            # so that jax will reuse the memory buffer for outputs.
            # This will reduce the memory usage of the training function by
            # half.
            @partial(jax.jit, donate_argnames="state")
            def compiled_test_step(state, data):
                return test_step(state, data)

            self.test_function = compiled_test_step

        else:
            self.test_function = test_step

    def make_predict_function(self, force=False):
        if self.predict_function is not None and not force:
            return self.predict_function

        def one_predict_step(state, data):
            data = data[0]
            return self.predict_step(state, data)

        def multi_predict_steps(state, data):
            outputs, state = one_predict_step(state, data[:1])

            for single_step_data in data[1:]:
                step_outputs, state = one_predict_step(
                    state,
                    [single_step_data],
                )
                outputs = tree.map_structure(
                    lambda t1, t2: jax.numpy.concatenate([t1, t2]),
                    outputs,
                    step_outputs,
                )
            return outputs, state

        if self.steps_per_execution > 1:
            predict_step = multi_predict_steps
        else:
            predict_step = one_predict_step

        if not self.run_eagerly and self.jit_compile:

            @jax.jit
            def compiled_predict_step(state, data):
                return predict_step(state, data)

            self.predict_function = compiled_predict_step

        else:
            self.predict_function = predict_step

    @traceback_utils.filter_traceback
    def fit(
        self,
        x=None,
        y=None,
        batch_size=None,
        epochs=1,
        verbose="auto",
        callbacks=None,
        validation_split=0.0,
        validation_data=None,
        shuffle=True,
        class_weight=None,
        sample_weight=None,
        initial_epoch=0,
        steps_per_epoch=None,
        validation_steps=None,
        validation_batch_size=None,
        validation_freq=1,
    ):
        self._assert_compile_called("fit")
        # TODO: respect compiled trainable state
        self._eval_epoch_iterator = None
        if validation_split and validation_data is None:
            # Create the validation data using the training data. Only supported
            # for TF/numpy/jax arrays.
            (
                x,
                y,
                sample_weight,
            ), validation_data = data_adapter_utils.train_validation_split(
                (x, y, sample_weight), validation_split=validation_split
            )

        if validation_data:
            (
                val_x,
                val_y,
                val_sample_weight,
            ) = data_adapter_utils.unpack_x_y_sample_weight(validation_data)

        # Create an iterator that yields batches for one epoch.
        epoch_iterator = JAXEpochIterator(
            x=x,
            y=y,
            sample_weight=sample_weight,
            batch_size=batch_size,
            steps_per_epoch=steps_per_epoch,
            shuffle=shuffle,
            class_weight=class_weight,
            steps_per_execution=self.steps_per_execution,
        )

        self._symbolic_build(iterator=epoch_iterator)

        # Container that configures and calls callbacks.
        if not isinstance(callbacks, callbacks_module.CallbackList):
            callbacks = callbacks_module.CallbackList(
                callbacks,
                add_history=True,
                add_progbar=verbose != 0,
                verbose=verbose,
                epochs=epochs,
                steps=epoch_iterator.num_batches,
                model=self,
            )
        self._record_training_state_sharding_spec()

        self.make_train_function()
        self.stop_training = False
        callbacks.on_train_begin()

        for epoch in range(initial_epoch, epochs):
            self.reset_metrics()
            callbacks.on_epoch_begin(epoch)

            trainable_variables = [v.value for v in self.trainable_variables]
            non_trainable_variables = [
                v.value for v in self.non_trainable_variables
            ]
            optimizer_variables = [v.value for v in self.optimizer.variables]
            metrics_variables = [v.value for v in self.metrics_variables]

            self._purge_model_variables()
            for step, data in epoch_iterator.enumerate_epoch():
                # Callbacks
                callbacks.on_train_batch_begin(step)

                # Train step
                state = (
                    trainable_variables,
                    non_trainable_variables,
                    optimizer_variables,
                    metrics_variables,
                )
                logs, state = self.train_function(state, data)
                (
                    trainable_variables,
                    non_trainable_variables,
                    optimizer_variables,
                    metrics_variables,
                ) = state

                # Setting _jax_state enables callbacks to force a state sync
                # if they need to.
                self._jax_state = {
                    "trainable_variables": trainable_variables,
                    "non_trainable_variables": non_trainable_variables,
                    "optimizer_variables": optimizer_variables,
                    "metrics_variables": metrics_variables,
                }

                # Callbacks
                callbacks.on_train_batch_end(step, self._pythonify_logs(logs))
                if self.stop_training:
                    break

            # Reattach state to model variables.
            # NOTE: doing this after each step would be a big performance
            # bottleneck.
            self.jax_state_sync()

            # Override with model metrics instead of last step logs
            # The jax spmd_mode is need for multi-process context, since the
            # metrics values are replicated, and we don't want to do a all
            # gather, and only need the local copy of the value.
            with jax.spmd_mode("allow_all"):
                epoch_logs = self.get_metrics_result()

            # Run validation.
            if validation_data and self._should_eval(epoch, validation_freq):
                # Create JAXEpochIterator for evaluation and cache it.
                if getattr(self, "_eval_epoch_iterator", None) is None:
                    self._eval_epoch_iterator = JAXEpochIterator(
                        x=val_x,
                        y=val_y,
                        sample_weight=val_sample_weight,
                        batch_size=validation_batch_size or batch_size,
                        steps_per_execution=self.steps_per_execution,
                        steps_per_epoch=validation_steps,
                        shuffle=False,
                    )
                val_logs = self.evaluate(
                    x=val_x,
                    y=val_y,
                    sample_weight=val_sample_weight,
                    batch_size=validation_batch_size or batch_size,
                    steps=validation_steps,
                    callbacks=callbacks,
                    return_dict=True,
                    _use_cached_eval_dataset=True,
                )
                val_logs = {
                    "val_" + name: val for name, val in val_logs.items()
                }
                epoch_logs.update(val_logs)

            callbacks.on_epoch_end(epoch, epoch_logs)
            training_logs = epoch_logs
            if self.stop_training:
                break

        if (
            isinstance(self.optimizer, optimizers_module.Optimizer)
            and epochs > 0
        ):
            self.optimizer.finalize_variable_values(self.trainable_weights)

        # If _eval_epoch_iterator exists, delete it after all epochs are done.
        if getattr(self, "_eval_epoch_iterator", None) is not None:
            del self._eval_epoch_iterator
        callbacks.on_train_end(logs=training_logs)
        self._jax_state = None
        return self.history

    @traceback_utils.filter_traceback
    def evaluate(
        self,
        x=None,
        y=None,
        batch_size=None,
        verbose="auto",
        sample_weight=None,
        steps=None,
        callbacks=None,
        return_dict=False,
        **kwargs,
    ):
        self._assert_compile_called("evaluate")
        # TODO: respect compiled trainable state
        use_cached_eval_dataset = kwargs.pop("_use_cached_eval_dataset", False)
        if kwargs:
            raise ValueError(f"Arguments not recognized: {kwargs}")

        if use_cached_eval_dataset:
            epoch_iterator = self._eval_epoch_iterator
        else:
            # Create an iterator that yields batches of input/target data.
            epoch_iterator = JAXEpochIterator(
                x=x,
                y=y,
                sample_weight=sample_weight,
                batch_size=batch_size,
                steps_per_epoch=steps,
                shuffle=False,
                steps_per_execution=self.steps_per_execution,
            )

        self._symbolic_build(iterator=epoch_iterator)

        # Container that configures and calls callbacks.
        if not isinstance(callbacks, callbacks_module.CallbackList):
            callbacks = callbacks_module.CallbackList(
                callbacks,
                add_history=True,
                add_progbar=verbose != 0,
                verbose=verbose,
                epochs=1,
                steps=epoch_iterator.num_batches,
                model=self,
            )
        self._record_training_state_sharding_spec()

        self.make_test_function()
        self.stop_evaluating = False
        callbacks.on_test_begin()
        logs = None
        self.reset_metrics()

        trainable_variables = [v.value for v in self.trainable_variables]
        non_trainable_variables = [
            v.value for v in self.non_trainable_variables
        ]
        metrics_variables = [v.value for v in self.metrics_variables]

        self._purge_model_variables(optimizer_variables=False)
        for step, data in epoch_iterator.enumerate_epoch():
            callbacks.on_test_batch_begin(step)

            state = (
                trainable_variables,
                non_trainable_variables,
                metrics_variables,
            )
            logs, state = self.test_function(state, data)
            (
                trainable_variables,
                non_trainable_variables,
                metrics_variables,
            ) = state

            # Setting _jax_state enables callbacks to force a state sync
            # if they need to.
            self._jax_state = {
                # I wouldn't recommend modifying non-trainable model state
                # during evaluate(), but it's allowed.
                "trainable_variables": trainable_variables,
                "non_trainable_variables": non_trainable_variables,
                "metrics_variables": metrics_variables,
            }
            callbacks.on_test_batch_end(step, self._pythonify_logs(logs))
            if self.stop_evaluating:
                break

        # Reattach state back to model.
        self.jax_state_sync()

        # The jax spmd_mode is need for multi-process context, since the
        # metrics values are replicated, and we don't want to do a all
        # gather, and only need the local copy of the value.
        with jax.spmd_mode("allow_all"):
            logs = self.get_metrics_result()
        callbacks.on_test_end(logs)
        self._jax_state = None
        if return_dict:
            return logs
        return self._flatten_metrics_in_order(logs)

    @traceback_utils.filter_traceback
    def predict(
        self, x, batch_size=None, verbose="auto", steps=None, callbacks=None
    ):
        # Create an iterator that yields batches of input data.
        epoch_iterator = JAXEpochIterator(
            x=x,
            batch_size=batch_size,
            steps_per_epoch=steps,
            shuffle=False,
            steps_per_execution=self.steps_per_execution,
        )

        if not all(layer.built for layer in self._flatten_layers()):
            # Build the model on one batch of data.
            for _, data in epoch_iterator.enumerate_epoch():
                # Build model
                x, _, _ = data_adapter_utils.unpack_x_y_sample_weight(data[0])
                with backend.StatelessScope():
                    self(x)
                break

        # Container that configures and calls callbacks.
        if not isinstance(callbacks, callbacks_module.CallbackList):
            callbacks = callbacks_module.CallbackList(
                callbacks,
                add_history=True,
                add_progbar=verbose != 0,
                verbose=verbose,
                epochs=1,
                steps=epoch_iterator.num_batches,
                model=self,
            )
        self._record_training_state_sharding_spec()

        self.make_predict_function()
        self.stop_predicting = False
        callbacks.on_predict_begin()

        def append_to_outputs(batch_outputs, outputs):
            if outputs is None:
                outputs = tree.map_structure(
                    lambda batch_output: [batch_output],
                    batch_outputs,
                )
            else:
                tree.map_structure_up_to(
                    batch_outputs,
                    lambda output, batch_output: output.append(batch_output),
                    outputs,
                    batch_outputs,
                )
            return outputs

        trainable_variables = [v.value for v in self.trainable_variables]
        non_trainable_variables = [
            v.value for v in self.non_trainable_variables
        ]
        self._purge_model_variables(
            optimizer_variables=False, metric_variables=False
        )
        state = (trainable_variables, non_trainable_variables)
        outputs = None
        for step, x in epoch_iterator.enumerate_epoch():
            callbacks.on_predict_batch_begin(step)
            batch_outputs, state = self.predict_function(state, x)
            outputs = append_to_outputs(batch_outputs, outputs)
            callbacks.on_predict_batch_end(step, {"outputs": batch_outputs})
            if self.stop_predicting:
                break

        self._jax_state = {
            # I wouldn't recommend modifying non-trainable model state
            # during predict(), but it's allowed.
            "trainable_variables": state[0],
            "non_trainable_variables": state[1],
        }
        self.jax_state_sync()
        callbacks.on_predict_end()
        self._jax_state = None
        return tree.map_structure_up_to(batch_outputs, np.concatenate, outputs)

    def train_on_batch(
        self,
        x,
        y=None,
        sample_weight=None,
        class_weight=None,
        return_dict=False,
    ):
        self._assert_compile_called("train_on_batch")
        if class_weight is not None:
            if sample_weight is not None:
                raise ValueError(
                    "Arguments `sample_weight` and `class_weight` "
                    "cannot be specified at the same time. "
                    f"Received: sample_weight={sample_weight}, "
                    f"class_weight={class_weight}"
                )
            sample_weight = data_adapter_utils.class_weight_to_sample_weights(
                y, class_weight
            )
        data = (x, y, sample_weight)
        data = _distribute_data(data)

        # Maybe build model
        self._symbolic_build(data_batch=data)
        self._record_training_state_sharding_spec()
        self.make_train_function()

        # Train step
        trainable_variables = [v.value for v in self.trainable_variables]
        non_trainable_variables = [
            v.value for v in self.non_trainable_variables
        ]
        optimizer_variables = [v.value for v in self.optimizer.variables]
        metrics_variables = [v.value for v in self.metrics_variables]
        state = (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
            metrics_variables,
        )
        logs, state = self.train_function(state, [data])

        # State sync
        (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
            metrics_variables,
        ) = state
        self._jax_state = {
            "trainable_variables": trainable_variables,
            "non_trainable_variables": non_trainable_variables,
            "optimizer_variables": optimizer_variables,
            "metrics_variables": metrics_variables,
        }
        self.jax_state_sync()

        # Format return values
        logs = tree.map_structure(lambda x: np.array(x), logs)
        if return_dict:
            return logs
        return self._flatten_metrics_in_order(logs)

    def test_on_batch(
        self,
        x,
        y=None,
        sample_weight=None,
        return_dict=False,
    ):
        self._assert_compile_called("test_on_batch")

        data = (x, y, sample_weight)
        data = _distribute_data(data)
        # Maybe build model
        self._symbolic_build(data_batch=data)
        self._record_training_state_sharding_spec()
        self.make_test_function()

        # Test step
        trainable_variables = [v.value for v in self.trainable_variables]
        non_trainable_variables = [
            v.value for v in self.non_trainable_variables
        ]
        metrics_variables = [v.value for v in self.metrics_variables]
        state = (
            trainable_variables,
            non_trainable_variables,
            metrics_variables,
        )
        logs, state = self.test_function(state, [data])

        # State sync
        trainable_variables, non_trainable_variables, metrics_variables = state
        self._jax_state = {
            "trainable_variables": trainable_variables,
            "non_trainable_variables": non_trainable_variables,
            "metrics_variables": metrics_variables,
        }
        self.jax_state_sync()

        # Format return values.
        logs = tree.map_structure(lambda x: np.array(x), logs)
        if return_dict:
            return logs
        return self._flatten_metrics_in_order(logs)

    def predict_on_batch(self, x):
        if not all(layer.built for layer in self._flatten_layers()):
            # Build model
            with backend.StatelessScope():
                self(x)
        self._record_training_state_sharding_spec()
        self.make_predict_function()

        trainable_variables = [v.value for v in self.trainable_variables]
        non_trainable_variables = [
            v.value for v in self.non_trainable_variables
        ]
        state = (trainable_variables, non_trainable_variables)
        batch_outputs, state = self.predict_function(state, [(x,)])
        batch_outputs = tree.map_structure(lambda x: np.array(x), batch_outputs)
        return batch_outputs

    def jax_state_sync(self):
        if not getattr(self, "_jax_state", None):
            return

        trainable_variables = self._jax_state.get("trainable_variables", None)
        non_trainable_variables = self._jax_state.get(
            "non_trainable_variables", None
        )
        optimizer_variables = self._jax_state.get("optimizer_variables", None)
        metrics_variables = self._jax_state.get("metrics_variables", None)
        if trainable_variables:
            for ref_v, v in zip(self.trainable_variables, trainable_variables):
                ref_v.assign(v)
        if non_trainable_variables:
            for ref_v, v in zip(
                self.non_trainable_variables, non_trainable_variables
            ):
                ref_v.assign(v)
        if optimizer_variables:
            for ref_v, v in zip(self.optimizer.variables, optimizer_variables):
                ref_v.assign(v)
        if metrics_variables:
            for ref_v, v in zip(self.metrics_variables, metrics_variables):
                ref_v.assign(v)

    def _record_training_state_sharding_spec(self):
        self._trainable_variable_shardings = [
            v.value.sharding for v in self.trainable_variables
        ]
        self._non_trainable_variable_shardings = [
            v.value.sharding for v in self.non_trainable_variables
        ]
        if hasattr(self, "optimizer") and self.optimizer is not None:
            self._optimizer_variable_shardings = [
                v.value.sharding for v in self.optimizer.variables
            ]
        else:
            self._optimizer_variable_shardings = []
        self._metrics_variable_shardings = [
            v.value.sharding for v in self.metrics_variables
        ]

    def _enforce_jax_state_sharding(
        self,
        trainable_variables=None,
        non_trainable_variables=None,
        optimizer_variables=None,
        metrics_variables=None,
    ):
        """Enforce the sharding spec constraint for all the training state.

        Since the output of the train/eval step will be used as inputs to next
        step, we need to ensure that they have the same sharding spec, so that
        jax.jit won't have to recompile the train/eval function.

        Note that this function will also rely on the recorded sharding spec
        for each of states.

        This function is expected to be called within the jitted train/eval
        function, especially around the end of the function.
        """
        trainable_variables = trainable_variables or []
        non_trainable_variables = non_trainable_variables or []
        optimizer_variables = optimizer_variables or []
        metrics_variables = metrics_variables or []

        for i in range(len(trainable_variables)):
            trainable_variables[i] = jax.lax.with_sharding_constraint(
                trainable_variables[i], self._trainable_variable_shardings[i]
            )
        for i in range(len(non_trainable_variables)):
            non_trainable_variables[i] = jax.lax.with_sharding_constraint(
                non_trainable_variables[i],
                self._non_trainable_variable_shardings[i],
            )
        for i in range(len(optimizer_variables)):
            optimizer_variables[i] = jax.lax.with_sharding_constraint(
                optimizer_variables[i], self._optimizer_variable_shardings[i]
            )
        for i in range(len(metrics_variables)):
            metrics_variables[i] = jax.lax.with_sharding_constraint(
                metrics_variables[i], self._metrics_variable_shardings[i]
            )
        return (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
            metrics_variables,
        )

    def _purge_model_variables(
        self,
        trainable_variables=True,
        non_trainable_variables=True,
        optimizer_variables=True,
        metric_variables=True,
    ):
        """Remove all the model variable for memory saving.

        During JAX training, since the training function are stateless, we have
        to pass in and get the model weights over and over, during which the
        copy of the weights that attached to the KerasVariable are still and
        occupying extra memory. We remove those variable to save memory (for
        better memory utilization) at the beginning of the epoch, and reattach
        the value back to variables at the end of the epoch, via
        `jax_state_sync()`.
        """
        if trainable_variables:
            for v in self.trainable_variables:
                v._value = None
        if non_trainable_variables:
            for v in self.non_trainable_variables:
                v._value = None
        if optimizer_variables:
            for v in self.optimizer.variables:
                v._value = None
        if metric_variables:
            for v in self.metrics_variables:
                v._value = None


def _distribute_data(data):
    distribution = distribution_lib.distribution()
    if distribution is not None:

        def distribute_single_value(d):
            layout = distribution.get_data_layout(d.shape)
            return jax_distribution_lib.distribute_data_input(d, layout)

        return jax.tree_util.tree_map(distribute_single_value, data)
    else:
        return jax.tree_util.tree_map(jax.device_put, data)


class JAXEpochIterator(EpochIterator):
    def _get_iterator(self):
        return self._prefetch_numpy_iterator(
            self.data_adapter.get_jax_iterator()
        )

    def _prefetch_numpy_iterator(self, numpy_iterator):
        """Shard and prefetch batches on device.

        Most of the implementation has been borrowed from
        `flax.jax_utils.prefetch_to_device`

        This utility takes an iterator and returns a new iterator which fills an
        on device prefetch buffer. Eager prefetching can improve the performance
        of training loops significantly by overlapping compute and data
        transfer.
        """
        queue = collections.deque()

        # If you're training on GPUs, 2 is generally the best choice because
        # this guarantees that you can overlap a training step on GPU with a
        # data prefetch step on CPU.
        def enqueue(n=2):
            for data in itertools.islice(numpy_iterator, n):
                queue.append(_distribute_data(data))

        enqueue(n=2)  # TODO: should we make `n` configurable?
        while queue:
            yield queue.popleft()
            enqueue(1)
