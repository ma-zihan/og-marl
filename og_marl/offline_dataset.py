# Copyright 2023 InstaDeep Ltd. All rights reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
import tensorflow as tf
from collections import namedtuple
import tree

Sample = namedtuple('Sample', ['observations', 'actions', 'rewards', 'done', 'episode_return', 'legal_actions', 'env_state', 'zero_padding_mask'])

def get_schema_dtypes(environment):
    act_type = list(environment.action_spaces.values())[0].dtype
    schema = {}
    for agent in environment.possible_agents:
        schema[agent + "_observations"] = tf.float32
        schema[agent + "_legal_actions"] = tf.float32
        schema[agent + "_actions"] = act_type
        schema[agent + "_rewards"] = tf.float32
        schema[agent + "_discounts"] = tf.float32

    ## Extras
    # Zero-padding mask
    schema["zero_padding_mask"] = tf.float32

    # Env state
    schema["env_state"] = tf.float32

    # Episode return
    schema["episode_return"] = tf.float32

    return schema

class OfflineMARLDataset:
    def __init__(
        self,
        environment,
        path_to_dataset,
        num_parallel_calls=None
    ):
        self._environment = environment
        self._schema = get_schema_dtypes(environment)
        self._agents = environment.possible_agents

        file_path = Path(path_to_dataset)
        filenames = [
            str(file_name) for file_name in file_path.glob("**/*.tfrecord")
        ]
        filename_dataset = tf.data.Dataset.from_tensor_slices(filenames)
        self._tf_dataset = filename_dataset.interleave(
            lambda x: tf.data.TFRecordDataset(x, compression_type="GZIP").map(
                self._decode_fn
            ),
            cycle_length=None,
            num_parallel_calls=num_parallel_calls,
            deterministic=False,
            block_length=None,
        )

    def get_sequence_length(self):
        for sample in self._tf_dataset:
            T = sample["mask"].shape[0]
            break
        return T

    def _decode_fn(self, record_bytes):
        example = tf.io.parse_single_example(
            record_bytes,
            tree.map_structure(
                lambda x: tf.io.FixedLenFeature([], dtype=tf.string), self._schema
            ),
        )

        for key, dtype in self._schema.items():
            example[key] = tf.io.parse_tensor(example[key], dtype)

        sample = {}
        for agent in self._agents:
            sample[f"{agent}_observations"] = example[f"{agent}_observations"]
            sample[f"{agent}_actions"] = example[f"{agent}_actions"]
            sample[f"{agent}_rewards"] = example[f"{agent}_rewards"]
            sample[f"{agent}_terminals"] = 1 - example[f"{agent}_discounts"]
            sample[f"{agent}_truncations"] = tf.zeros_like(example[f"{agent}_discounts"])
            sample[f"{agent}_legals"] = example[f"{agent}_legal_actions"]
            
        sample["mask"] = example["zero_padding_mask"]
        sample["state"] = example["env_state"]

        return sample

    def __getattr__(self, name):
        """Expose any other attributes of the underlying environment.

        Args:
            name (str): attribute.

        Returns:
            Any: return attribute from env or underlying env.
        """
        if hasattr(self.__class__, name):
            return self.__getattribute__(name)
        else:
            return getattr(self._tf_dataset, name)