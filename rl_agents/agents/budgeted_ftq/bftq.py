import copy
from multiprocessing.pool import Pool

import numpy as np
from gym import logger
import torch
import torch.nn.functional as F
from torch import tensor

from rl_agents.agents.budgeted_ftq.budgeted_utils import TransitionBFTQ, compute_convex_hull_from_values, \
    optimal_mixture
from rl_agents.agents.budgeted_ftq.models import loss_function_factory, optimizer_factory
from rl_agents.agents.utils import ReplayMemory, near_split


class BudgetedFittedQ(object):
    def __init__(self, policy_network, config):
        self.config = config

        # Load configs
        try:
            self.betas_for_duplication = eval(self.config["betas_for_duplication"])
        except TypeError:
            self.betas_for_duplication = self.config["betas_for_duplication"]
        try:
            self.betas_for_discretisation = eval(self.config["betas_for_discretisation"])
        except TypeError:
            self.betas_for_discretisation = self.config["betas_for_discretisation"]
        self.loss_function = loss_function_factory(self.config["loss_function"])
        self.loss_function_c = loss_function_factory(self.config["loss_function_c"])
        self.device = self.config["device"]

        # Load network
        self._value_network = policy_network
        self._value_network = self._value_network.to(self.device)
        self.n_actions = self._value_network.predict.out_features // 2

        self.memory = ReplayMemory(transition_type=TransitionBFTQ)
        self.optimizer = None
        self.epoch = 0
        self.reset()

    def push(self, state, action, reward, next_state, terminal, cost, beta=None):
        """
            Push a transition into the replay memory.
        """
        action = torch.tensor([[action]], dtype=torch.long)
        reward = torch.tensor([reward], dtype=torch.float)
        terminal = torch.tensor([terminal], dtype=torch.bool)
        cost = torch.tensor([cost], dtype=torch.float)
        state = torch.tensor([[state]], dtype=torch.float)
        next_state = torch.tensor([[next_state]], dtype=torch.float)

        # Data augmentation for (potentially missing) budget values
        if self.betas_for_duplication:
            for beta_d in self.betas_for_duplication:
                if beta:  # If the transition already has a beta, augment data by altering it.
                    beta_d = torch.tensor([[[beta_d * beta]]], dtype=torch.float)
                else:  # Otherwise, simply set new betas
                    beta_d = torch.tensor([[[beta_d]]], dtype=torch.float)
                self.memory.push(state, action, reward, next_state, terminal, cost, beta_d)
        else:
            beta = torch.tensor([[[beta]]], dtype=torch.float)
            self.memory.push(state, action, reward, next_state, terminal, cost, beta)

    def run(self):
        """
            Run BFTQ on the batch of transitions in memory.

            We fit a model for the optimal reward-cost state-budget-action values Qr and Qc.
            The BFTQ epoch is repeated until convergence or timeout.
        :return: the obtained value network Qr, Qc
        """
        for self.epoch in range(self.config["max_ftq_epochs"]):
            delta = self._epoch()
            if delta < self.config["delta_stop"]:
                break
        return self._value_network

    def _epoch(self):
        """
            Run a single epoch of BFTQ.

            This is similar to a fitted value iteration:
            1. Bootstrap the targets for Qr, Qc using the constrained Bellman Operator
            2. Fit the Qr, Qc model to the targets
        :return: delta, the Bellman residual between the model and target values
        """
        states_betas, actions, rewards, costs, next_states, betas, terminals = self._zip_batch()
        target_r, target_c = self.compute_targets(rewards, costs, next_states, betas, terminals)
        return self._fit(states_betas, actions, target_r, target_c)

    def _zip_batch(self):
        """
            Convert the batch of transitions to several tensors of states, actions, rewards, etc.
        :return: state-beta, state, action, reward, constraint, next_state, beta, terminal batches
        """
        batch = self.memory.memory
        self.size_batch = len(batch)
        zipped = TransitionBFTQ(*zip(*batch))
        actions = torch.cat(zipped.action).to(self.device)
        rewards = torch.cat(zipped.reward).to(self.device)
        terminals = torch.cat(zipped.terminal).to(self.device)
        costs = torch.cat(zipped.cost).to(self.device)

        beta_batch = torch.cat(zipped.beta).to(self.device)
        state_batch = torch.cat(zipped.state).to(self.device)
        next_state_batch = torch.cat(zipped.next_state).to(self.device)
        state_beta_batch = torch.cat((state_batch, beta_batch), dim=2).to(self.device)

        # Batch normalization
        mean = torch.mean(state_beta_batch, 0).to(self.device)
        std = torch.std(state_beta_batch, 0).to(self.device)
        self._value_network.set_normalization_params(mean, std)

        return state_beta_batch, actions, rewards, costs, next_state_batch, beta_batch, \
            terminals

    def compute_targets(self, rewards, costs, next_states, betas, terminals):
        """
            Compute target values by applying constrained Bellman operator
        :param rewards: batch of rewards
        :param costs: batch of costs
        :param next_states: batch of next states
        :param betas: batch of budgets
        :param terminals: batch of terminations
        :return: target values
        """
        with torch.no_grad():
            next_rewards, next_costs = self.constrained_next_values(next_states, betas, terminals)
            target_r = rewards + self.config["gamma"] * next_rewards
            target_c = costs + self.config["gamma_c"] * next_costs

            if self.config["clamp_Qc"] is not None:
                logger.info("Clamp target costs")
                target_c = torch.clamp(target_c, min=self.config["clamp_Qc"][0], max=self.config["clamp_Qc"][1])
            torch.cuda.empty_cache()
        return target_r, target_c

    def constrained_next_values(self, next_states, betas, terminals):
        """
            Boostrap the (qr, qc) values at next states, following optimal policies under budget constraints.

            The model is evaluated for optimal mixtures of actions & budgets that fulfill the cost constraints.

        :param next_states: batch of next states
        :param betas: batch of budgets
        :param terminals: batch of terminations
        :return: qr and qc at the next states, following optimal mixtures
        """
        # Initialisation
        next_rewards = torch.zeros(len(next_states), device=self.device)
        next_costs = torch.zeros(len(next_states), device=self.device)
        if self.epoch == 0:
            return next_rewards, next_costs

        # Select non-final next states
        next_states_nf = next_states[1 - terminals]
        betas_nf = betas[1 - terminals]

        # Forward pass of the model Qr, Qc
        q_values = self.compute_next_values(next_states_nf)

        # Compute hulls H
        hulls = self.compute_all_hulls(q_values, len(next_states_nf))

        # Compute optimal mixture policies satisfying budgets beta
        mixtures = self.compute_all_optimal_mixtures(hulls, betas_nf)

        # Compute expected costs and rewards of these mixtures
        next_rewards_nf = torch.zeros(len(next_states_nf), device=self.device)
        next_costs_nf = torch.zeros(len(next_states_nf), device=self.device)
        for i, mix in enumerate(mixtures):
            next_rewards_nf[i] = (1 - mix.probability_sup) * mix.inf.qr + mix.probability_sup * mix.sup.qr
            next_costs_nf[i] = (1 - mix.probability_sup) * mix.inf.qc + mix.probability_sup * mix.sup.qc
        next_rewards[1 - terminals] = next_rewards_nf
        next_costs[1 - terminals] = next_costs_nf

        torch.cuda.empty_cache()
        return next_rewards, next_costs

    def compute_next_values(self, next_states):
        """
            Compute Q(s, beta) with a single forward pass
        :param next_states: batch of next state
        :return: Q values at next states
        """
        # Compute the cartesian product sb of all next states s with all budgets b
        ss = next_states.squeeze().repeat((1, len(self.betas_for_discretisation))) \
            .view((len(next_states) * len(self.betas_for_discretisation), self._value_network.size_state))
        bb = torch.from_numpy(self.betas_for_discretisation).float().unsqueeze(1).to(device=self.device)
        bb = bb.repeat((len(next_states), 1))
        sb = torch.cat((ss, bb), dim=1).unsqueeze(1)

        # To avoid spikes in memory, we actually split the batch in several minibatches
        batch_sizes = near_split(x=len(sb), num_bins=self.config["split_batches"])
        q_values = []
        for minibatch in range(self.config["split_batches"]):
            mini_batch = sb[sum(batch_sizes[:minibatch]):sum(batch_sizes[:minibatch + 1])]
            q_values.append(self._value_network(mini_batch))
            torch.cuda.empty_cache()
        return torch.cat(q_values).detach().cpu().numpy()

    def compute_all_hulls(self, q_values, states_count):
        """
            Parallel computing of hulls
        """
        n_beta = len(self.betas_for_discretisation)
        hull_params = [(q_values[state * n_beta: (state + 1) * n_beta],
                        self.betas_for_discretisation,
                        self.config["hull_options"],
                        self.config["clamp_Qc"])
                       for state in range(states_count)]
        with Pool(self.config["cpu_processes"]) as p:
            results = p.starmap(compute_convex_hull_from_values, hull_params)
            hulls, _, _, _ = zip(*results)
        torch.cuda.empty_cache()
        return hulls

    def compute_all_optimal_mixtures(self, b_batch, hulls):
        """
            Parallel computing of optimal mixtures
        """
        with Pool(self.config["cpu_processes"]) as p:
            optimal_policies = p.starmap(optimal_mixture,
                                         [(hulls[i], beta.detach().item()) for i, beta in enumerate(b_batch)])
        return optimal_policies

    def _fit(self, states_betas, actions, target_r, target_c):
        """
            Fit a network Q(state, action, beta) = (Qr, Qc) to target values
        :param states_betas: batch of states and betas
        :param actions: batch of actions
        :param target_r: batch of target reward-values
        :param target_c: batch of target cost-values
        :return: the Bellman residual delta between the model and target values
        """
        # Initial Bellman residual
        with torch.no_grad():
            delta = self._compute_loss(states_betas, actions, target_r, target_c).detach().item()
            torch.cuda.empty_cache()

        # Reset network
        if self.config["reset_network_each_epoch"]:
            self.reset_network()

        # Gradient descent
        losses = []
        for nn_epoch in range(self.config["max_nn_epoch"]):
            loss = self._gradient_step(states_betas, actions, target_r, target_c)
            losses.append(loss)
        torch.cuda.empty_cache()

        return delta

    def _compute_loss(self, states_betas, actions, target_r, target_c):
        """
            Compute the loss between the model values and target values
        :param states_betas: input state-beta batch
        :param actions: input actions batch
        :param target_r: target qr
        :param target_c: target qc
        :return: the weighted loss for expected rewards and costs
        """
        values = self._value_network(states_betas)
        qr = values.gather(1, actions)
        qc = values.gather(1, actions + self.n_actions)
        loss_qc = self.loss_function_c(qc, target_c.unsqueeze(1))
        loss_qr = self.loss_function(qr, target_r.unsqueeze(1))
        w_r, w_c = self.config["weights_losses"]
        loss = w_c * loss_qc + w_r * loss_qr
        return loss

    def _gradient_step(self, states_betas, actions, target_r, target_c):
        loss = self._compute_loss(states_betas, actions, target_r, target_c)
        self.optimizer.zero_grad()
        loss.backward()
        for param in self._value_network.parameters():
            param.grad.data.clamp_(-1, 1)
        self.optimizer.step()
        return loss.detach().item()

    def save_policy(self, policy_path=None):
        if policy_path is None:
            policy_path = self.workspace / "policy.pt"
        logger.info("saving bftq policy at {}".format(policy_path))
        torch.save(self._value_network, policy_path)
        return policy_path

    def reset_network(self):
        self._value_network.reset()

    def reset(self, reset_weight=True):
        torch.cuda.empty_cache()
        if reset_weight:
            self.reset_network()
        self.optimizer = optimizer_factory(self.config["optimizer_type"],
                                           self._value_network.parameters(),
                                           self.config["learning_rate"],
                                           self.config["weight_decay"])
        self.epoch = 0