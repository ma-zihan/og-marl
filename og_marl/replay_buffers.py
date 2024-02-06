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

import numpy as np
import jax
import jax.numpy as jnp
import flashbax as fbx
import cpprb
import copy
import tree
from flashbax.buffers.trajectory_buffer import TrajectoryBufferState

class FlashbaxReplayBuffer:

    def __init__(self, sequence_length, max_size=50_000, batch_size=32, sample_period=1, seed=42):

        self._sequence_length = sequence_length
        self._max_size = max_size
        self._batch_size = batch_size

        # Flashbax buffer
        self._replay_buffer = fbx.make_trajectory_buffer(
            add_batch_size=1,
            sample_batch_size=batch_size,
            sample_sequence_length=sequence_length,
            period=sample_period,
            min_length_time_axis=1,
            max_size=max_size,
        )

        self._buffer_sample_fn = jax.jit(self._replay_buffer.sample)
        self._buffer_add_fn = jax.jit(self._replay_buffer.add)

        self._buffer_state = None
        self._rng_key = jax.random.PRNGKey(seed)

    def add(self, observations, actions, rewards, terminals, truncations, infos):
        timestep = {
            "observations": observations,
            "actions": actions,
            "rewards": rewards,
            "terminals": terminals,
            "truncations": truncations,
            "infos": infos
        }

        if self._buffer_state is None:
            self._buffer_state = self._replay_buffer.init(timestep)
        
        timestep = tree.map_structure(lambda x: np.expand_dims(np.expand_dims(x, axis=0), axis=0), timestep) # add batch dim
        self._buffer_state = self._buffer_add_fn(self._buffer_state, timestep)

    def sample(self):
        self._rng_key, sample_key = jax.random.split(self._rng_key,2)
        batch = self._buffer_sample_fn(self._buffer_state, sample_key)
        return batch.experience
    
    def populate_from_dataset(self, dataset):
        batch_size = 2048
        batched_dataset = dataset.raw_dataset.batch(batch_size)
        period = dataset.period
        max_episode_length = dataset.max_episode_length
        agents = dataset._agents

        experience = {
            "observations": {agent: [] for agent in agents},
            "actions": {agent: [] for agent in agents},
            "rewards": {agent: [] for agent in agents},
            "terminals": {agent: [] for agent in agents},
            "truncations": {agent: [] for agent in agents},
            "infos": {
                "legals": {agent: [] for agent in agents},
                "state": []
            }
        }

        episode = {
            "observations": {agent: [] for agent in agents},
            "actions": {agent: [] for agent in agents},
            "rewards": {agent: [] for agent in agents},
            "terminals": {agent: [] for agent in agents},
            "truncations": {agent: [] for agent in agents},
            "infos": {
                "legals": {agent: [] for agent in agents},
                "state": []
            }
        }

        episode_length = 0
        for batch in batched_dataset:
            mask = copy.deepcopy(batch["infos"]["mask"])
            B = mask.shape[0] # batch_size
            for idx in range(B):
                zero_padding_mask = mask[idx,:period]
                episode_length += np.sum(zero_padding_mask, dtype=int)

                for agent in agents:
                    episode["observations"][agent].append(batch["observations"][agent][idx, :period])
                    episode["actions"][agent].append(batch["actions"][agent][idx, :period])
                    episode["rewards"][agent].append(batch["rewards"][agent][idx, :period])
                    episode["terminals"][agent].append(batch["terminals"][agent][idx, :period])
                    episode["truncations"][agent].append(batch["truncations"][agent][idx, :period])
                    episode["infos"]["legals"][agent].append(batch["infos"]["legals"][agent][idx, :period])
                episode["infos"]["state"].append(batch["infos"]["state"][idx, :period])

                if (
                    int(list(episode["terminals"].values())[0][-1][-1]) == 1 # agent 0, last chunck, last timestep in chunk
                    or episode_length >= max_episode_length
                ):
                    for agent in agents:
                        experience["observations"][agent].append(np.concatenate(episode["observations"][agent], axis=0)[:episode_length])
                        experience["actions"][agent].append(np.concatenate(episode["actions"][agent], axis=0)[:episode_length])
                        experience["rewards"][agent].append(np.concatenate(episode["rewards"][agent], axis=0)[:episode_length])
                        experience["terminals"][agent].append(np.concatenate(episode["terminals"][agent], axis=0)[:episode_length])
                        experience["truncations"][agent].append(np.concatenate(episode["truncations"][agent], axis=0)[:episode_length])
                        experience["infos"]["legals"][agent].append(np.concatenate(episode["infos"]["legals"][agent], axis=0)[:episode_length])
                    experience["infos"]["state"].append(np.concatenate(episode["infos"]["state"], axis=0)[:episode_length])

                    # Clear episode
                    episode = {
                        "observations": {agent: [] for agent in agents},
                        "actions": {agent: [] for agent in agents},
                        "rewards": {agent: [] for agent in agents},
                        "terminals": {agent: [] for agent in agents},
                        "truncations": {agent: [] for agent in agents},
                        "infos": {
                            "legals": {agent: [] for agent in agents},
                            "state": []
                        }
                    }
                    episode_length = 0

        # Concatenate Episodes Together
        for agent in agents:
            experience["observations"][agent] = np.concatenate(experience["observations"][agent], axis=0)
            experience["actions"][agent] = np.concatenate(experience["actions"][agent], axis=0)
            experience["rewards"][agent] = np.concatenate(experience["rewards"][agent], axis=0)
            experience["terminals"][agent] = np.concatenate(experience["terminals"][agent], axis=0)
            experience["truncations"][agent] = np.concatenate(experience["truncations"][agent], axis=0)
            experience["infos"]["legals"][agent] = np.concatenate(experience["infos"]["legals"][agent], axis=0)
        experience["infos"]["state"] = np.concatenate(experience["infos"]["state"], axis=0)

        experience = jax.tree_map(lambda x: jnp.expand_dims(x, axis=0), experience)

        buffer_state = TrajectoryBufferState(experience=experience, is_full=jnp.array(False, dtype=bool), current_index=jnp.array(0))

        self._buffer_state = buffer_state

class SequenceCPPRB:

    def __init__(self, environment, sequence_length=20, max_size=10_000, batch_size=32):
        self._environment = environment
        self._sequence_length = sequence_length
        self._max_size = max_size
        self._batch_size = batch_size
        self._info_spec = self._environment.info_spec

        cpprb_env_dict = {}
        sequence_buffer = {}
        for agent in environment.possible_agents:
            obs_shape = self._environment.observation_spaces[agent].shape
            act_shape = self._environment.action_spaces[agent].shape

            cpprb_env_dict[f"{agent}_observations"] = {"shape": (sequence_length, *obs_shape)}
            cpprb_env_dict[f"{agent}_actions"] = {"shape": (sequence_length, *act_shape)}
            cpprb_env_dict[f"{agent}_rewards"] = {"shape": (sequence_length,)}
            cpprb_env_dict[f"{agent}_terminals"] = {"shape": (sequence_length,)}
            cpprb_env_dict[f"{agent}_truncations"] = {"shape": (sequence_length,)}

            sequence_buffer[f"{agent}_observations"] = np.zeros((sequence_length, *obs_shape), "float32")
            sequence_buffer[f"{agent}_actions"] = np.zeros((sequence_length, *act_shape), "float32")
            sequence_buffer[f"{agent}_rewards"] = np.zeros((sequence_length,), "float32")
            sequence_buffer[f"{agent}_terminals"] = np.zeros((sequence_length,), "float32")
            sequence_buffer[f"{agent}_truncations"] = np.zeros((sequence_length,), "float32")

            if "legals" in self._info_spec:
                legals_shape = self._info_spec["legals"][agent].shape
                cpprb_env_dict[f"{agent}_legals"] = {"shape": (sequence_length, *legals_shape)}
                sequence_buffer[f"{agent}_legals"] = np.zeros((sequence_length, *legals_shape), "float32")
        
        cpprb_env_dict["mask"] = {"shape": (sequence_length,)}
        sequence_buffer["mask"] = np.zeros((sequence_length,), "float32")

        if "state" in self._info_spec:
            state_shape = self._info_spec["state"].shape

            cpprb_env_dict["state"] = {"shape": (sequence_length, *state_shape)}
            sequence_buffer["state"] = np.zeros((sequence_length, *state_shape), "float32")

        self._cpprb = cpprb.ReplayBuffer(
            max_size,
            env_dict =cpprb_env_dict,
            default_dtype=np.float32
        )

        self._sequence_buffer = sequence_buffer

        self._t = 0

    def add(self, observations, actions, rewards, terminals, truncations, infos):

        for agent in self._environment.possible_agents:
            self._sequence_buffer[f"{agent}_observations"][self._t] = np.array(observations[agent], "float32")
            self._sequence_buffer[f"{agent}_actions"][self._t] = np.array(actions[agent], "float32")
            self._sequence_buffer[f"{agent}_rewards"][self._t] = np.array(rewards[agent], "float32")
            self._sequence_buffer[f"{agent}_terminals"][self._t] = np.array(terminals[agent], "float32")
            self._sequence_buffer[f"{agent}_truncations"][self._t] = np.array(truncations[agent], "float32")

            if "legals" in infos:
                self._sequence_buffer[f"{agent}_legals"][self._t] = np.array(infos["legals"][agent], "float32")

        self._sequence_buffer["mask"][self._t] = np.array(1, "float32")

        if "state" in infos:
            self._sequence_buffer["state"][self._t] = np.array(infos["state"], "float32")

        self._t += 1

        if self._t == self._sequence_length:
            self._push_to_cpprb()
            self._t = 0

    def end_of_episode(self):
        if self._t > 0:
            self._zero_pad()
            self._push_to_cpprb()

        self._cpprb.on_episode_end()

        self._t = 0

    def populate_from_dataset(self, dataset):
        dataset = dataset.batch(128)
        for batch in dataset:
            batch = tree.map_structure(lambda x: x.numpy(), batch)
            self._cpprb.add(**batch)
        print("Done")

    def _push_to_cpprb(self):
        self._cpprb.add(**self._sequence_buffer)

    def _zero_pad(self):
        for key, value in self._sequence_buffer.items():
            trailing_dims = value.shape[1:]
            zero_pad = np.zeros((self._sequence_length - self._t, *trailing_dims), "float32")
            self._sequence_buffer[key][self._t:] = zero_pad

    def __iter__(self):
        return self
    
    def __next__(self):
        cpprb_sample = self._cpprb.sample(self._batch_size)

        return cpprb_sample