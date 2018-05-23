from abc import ABC, abstractmethod

import numpy as np

from rl_agents.agents.abstract import AbstractAgent


class DQNAgent(AbstractAgent, ABC):
    def __init__(self):
        self.previous_state = None

    def act(self, state):
        self.previous_state = state
        _, optimal_action = self.get_state_value(state)
        return self.exploration_policy.epsilon_greedy(np.asscalar(optimal_action), self.env.action_space)

    @abstractmethod
    def get_batch_state_values(self, states):
        """
        Get the state-values of several states
        :param states: an array of states
        :return: values, actions: the optimal action values and corresponding indexes
        """
        raise NotImplementedError()

    @abstractmethod
    def get_batch_state_action_values(self, states):
        raise NotImplementedError()

    def get_state_value(self, state):
        values, actions = self.get_batch_state_values([state])
        return values[0], actions[0]

    def get_state_action_values(self, state):
        return self.get_batch_state_action_values([state])[0]