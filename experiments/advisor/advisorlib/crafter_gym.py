"""Gymnasium-compatible wrapper around crafter.Env.

Crafter ships a dm-env-shaped API. SB3 (and most modern RL code) expects
gymnasium. This bridges the two with no behavior change — just API
shape: reset returns (obs, info), step returns (obs, reward,
terminated, truncated, info), and the spaces are gymnasium types.
"""
from __future__ import annotations

import crafter
import gymnasium as gym
import numpy as np
from gymnasium import spaces


class CrafterGymEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, seed: int | None = None):
        super().__init__()
        self._env = crafter.Env(seed=seed)
        self.action_space = spaces.Discrete(self._env.action_space.n)
        self.observation_space = spaces.Box(
            low=0, high=255, shape=(64, 64, 3), dtype=np.uint8
        )
        self._last_info: dict = {}

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        obs = self._env.reset()
        self._last_info = {}
        return obs, {}

    def step(self, action: int):
        obs, reward, done, info = self._env.step(int(action))
        self._last_info = info
        # Crafter has no explicit truncation; treat done as terminal.
        return obs, float(reward), bool(done), False, info

    @property
    def last_info(self) -> dict:
        return self._last_info
