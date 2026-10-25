"""
core/state_machine.py — Explicit Assistant State Machine for JARVIS.

Defines the formal states, thread-safe transitions, listeners, and history tracking.

States:
  STARTING                  Initial boot / configuration / lightweight warm-up
  SLEEPING                  Wake-word listening (low resource mode)
  WAKE_WORD_LISTENING       Alias for SLEEPING
  LISTENING                 Active microphone, capturing user utterance
  THINKING                  LLM processing / planning response
  EXECUTING                 Running an action, tool, or browser / desktop command
  VERIFYING                 Perception / observation step verifying action outcome
  PAUSED_FOR_MANUAL_CONTROL Immediate pause when user physical input or manual takeover detected
  OFFLINE                   Network disconnected, waiting for reconnection
  STOPPED                   Emergency stopped or manually halted
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional


class AssistantState(enum.Enum):
    STARTING = "STARTING"
    SLEEPING = "SLEEPING"
    WAKE_WORD_LISTENING = "SLEEPING"   # semantic alias
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    PAUSED_FOR_MANUAL_CONTROL = "PAUSED_FOR_MANUAL_CONTROL"
    OFFLINE = "OFFLINE"
    STOPPED = "STOPPED"

    @classmethod
    def from_str(cls, name: str) -> AssistantState:
        normalized = (name or "").strip().upper()
        if normalized in ("WAKE_WORD_LISTENING", "SLEEP"):
            return cls.SLEEPING
        if normalized in ("PROCESSING", "REASONING"):
            return cls.THINKING
        try:
            return cls[normalized]
        except KeyError:
            return cls.STOPPED


@dataclass(frozen=True)
class StateTransition:
    from_state: AssistantState
    to_state: AssistantState
    timestamp: float
    reason: str


class StateMachine:
    """Thread-safe Assistant State Machine with validation and change events."""

    # Allowed transitions map: key -> set of permissible destinations
    _VALID_TRANSITIONS: dict[AssistantState, set[AssistantState]] = {
        AssistantState.STARTING: {
            AssistantState.SLEEPING,
            AssistantState.LISTENING,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
        },
        AssistantState.SLEEPING: {
            AssistantState.LISTENING,
            AssistantState.THINKING,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
            AssistantState.STARTING,
        },
        AssistantState.LISTENING: {
            AssistantState.THINKING,
            AssistantState.SLEEPING,
            AssistantState.PAUSED_FOR_MANUAL_CONTROL,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
        },
        AssistantState.THINKING: {
            AssistantState.EXECUTING,
            AssistantState.LISTENING,
            AssistantState.SLEEPING,
            AssistantState.PAUSED_FOR_MANUAL_CONTROL,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
        },
        AssistantState.EXECUTING: {
            AssistantState.VERIFYING,
            AssistantState.EXECUTING,
            AssistantState.LISTENING,
            AssistantState.SLEEPING,
            AssistantState.PAUSED_FOR_MANUAL_CONTROL,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
        },
        AssistantState.VERIFYING: {
            AssistantState.EXECUTING,
            AssistantState.LISTENING,
            AssistantState.SLEEPING,
            AssistantState.PAUSED_FOR_MANUAL_CONTROL,
            AssistantState.OFFLINE,
            AssistantState.STOPPED,
        },
        AssistantState.PAUSED_FOR_MANUAL_CONTROL: {
            AssistantState.EXECUTING,
            AssistantState.LISTENING,
            AssistantState.SLEEPING,
            AssistantState.STOPPED,
            AssistantState.OFFLINE,
        },
        AssistantState.OFFLINE: {
            AssistantState.STARTING,
            AssistantState.SLEEPING,
            AssistantState.LISTENING,
            AssistantState.STOPPED,
        },
        AssistantState.STOPPED: {
            AssistantState.STARTING,
            AssistantState.SLEEPING,
            AssistantState.LISTENING,
        },
    }

    def __init__(self, initial_state: AssistantState = AssistantState.STARTING):
        self._lock = threading.RLock()
        self._state: AssistantState = initial_state
        self._history: deque[StateTransition] = deque(maxlen=100)
        self._listeners: list[Callable[[AssistantState, AssistantState, str], None]] = []
        self._cancel_token = threading.Event()
        # Record initial creation
        self._history.append(
            StateTransition(
                from_state=initial_state,
                to_state=initial_state,
                timestamp=time.time(),
                reason="initialization",
            )
        )

    @property
    def current(self) -> AssistantState:
        with self._lock:
            return self._state

    @property
    def name(self) -> str:
        with self._lock:
            return self._state.name

    @property
    def is_active(self) -> bool:
        """True if actively listening, thinking, executing or verifying."""
        with self._lock:
            return self._state in (
                AssistantState.LISTENING,
                AssistantState.THINKING,
                AssistantState.EXECUTING,
                AssistantState.VERIFYING,
            )

    @property
    def is_executing(self) -> bool:
        with self._lock:
            return self._state in (AssistantState.EXECUTING, AssistantState.VERIFYING)

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._state == AssistantState.PAUSED_FOR_MANUAL_CONTROL

    @property
    def is_idle(self) -> bool:
        with self._lock:
            return self._state in (AssistantState.SLEEPING, AssistantState.STOPPED)

    @property
    def is_cancelled(self) -> bool:
        return self._cancel_token.is_set()

    def reset_cancellation(self) -> None:
        self._cancel_token.clear()

    def can_transition(self, target: AssistantState) -> bool:
        with self._lock:
            if target == self._state:
                return True
            # Emergency stops or pauses for manual control are ALWAYS permissible
            if target in (AssistantState.STOPPED, AssistantState.PAUSED_FOR_MANUAL_CONTROL):
                return True
            allowed = self._VALID_TRANSITIONS.get(self._state, set())
            return target in allowed

    def transition(self, target: AssistantState | str, reason: str = "") -> bool:
        """Transition to a target state with validation and notification."""
        if isinstance(target, str):
            target = AssistantState.from_str(target)

        with self._lock:
            from_state = self._state
            if from_state == target:
                return True

            # Check validity unless emergency stop or manual pause
            if not self.can_transition(target):
                # Fallback: force transition if moving to safety/stop
                if target not in (AssistantState.STOPPED, AssistantState.PAUSED_FOR_MANUAL_CONTROL):
                    return False

            if target in (AssistantState.STOPPED, AssistantState.PAUSED_FOR_MANUAL_CONTROL):
                self._cancel_token.set()
            elif from_state in (AssistantState.STOPPED, AssistantState.PAUSED_FOR_MANUAL_CONTROL):
                self._cancel_token.clear()

            self._state = target
            transition_record = StateTransition(
                from_state=from_state,
                to_state=target,
                timestamp=time.time(),
                reason=reason,
            )
            self._history.append(transition_record)
            listeners = list(self._listeners)

        # Notify outside the lock to prevent deadlock
        for callback in listeners:
            try:
                callback(from_state, target, reason)
            except Exception as e:
                print(f"[StateMachine] Listener error: {e}")

        return True

    def pause_for_manual_control(self, reason: str = "Physical user input detected") -> None:
        """Immediate pause when manual takeover occurs."""
        self._cancel_token.set()
        self.transition(AssistantState.PAUSED_FOR_MANUAL_CONTROL, reason=reason)

    def emergency_stop(self, reason: str = "Emergency stop triggered") -> None:
        """Emergency stop: halts active automation, flags cancellation, sets STOPPED."""
        self._cancel_token.set()
        self.transition(AssistantState.STOPPED, reason=reason)

    def add_listener(self, callback: Callable[[AssistantState, AssistantState, str], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[AssistantState, AssistantState, str], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def history(self) -> list[StateTransition]:
        with self._lock:
            return list(self._history)


# Global singleton instance
_GLOBAL_STATE_MACHINE: Optional[StateMachine] = None
_STATE_MACHINE_LOCK = threading.Lock()


def get_state_machine() -> StateMachine:
    global _GLOBAL_STATE_MACHINE
    with _STATE_MACHINE_LOCK:
        if _GLOBAL_STATE_MACHINE is None:
            _GLOBAL_STATE_MACHINE = StateMachine(AssistantState.STARTING)
        return _GLOBAL_STATE_MACHINE
