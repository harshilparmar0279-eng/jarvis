"""
actions/screen_agent.py — Autonomous Perception-Action Desktop Control for JARVIS.

Enables JARVIS to respond to:
  "Jarvis, take control of my screen and complete this task."

Architecture:
  • Controlled Perception-Action loop with observation, bounded execution, and verification.
  • Immediate Manual Takeover: Automatically pauses if the user physically moves the mouse or types.
  • Emergency Stop integration: Aborts immediately on hotkey or voice stop without needing LLM response.
  • Uses existing computer_control operations and screen_processor vision capture.
  • Outcome contract compliant (done / started / unverified / ask / failed).
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from typing import Any, Optional

from actions import computer_control
from actions.screen_processor import _capture_screen
from core import gemini, outcome
from core.emergency_stop import get_emergency_stop
from core.manual_takeover import get_takeover_monitor
from core.state_machine import AssistantState, get_state_machine


class ScreenAgentSession:
    """Manages an active multi-step autonomous screen control session."""

    def __init__(self):
        self._lock = threading.RLock()
        self._active = False
        self._task_description = ""
        self._current_step = 0
        self._max_steps = 15
        self._history: list[dict] = []
        self._target_window = ""
        self._last_observation = ""
        self._worker_thread: Optional[threading.Thread] = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._active

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            monitor = get_takeover_monitor()
            sm = get_state_machine()
            return {
                "active": self._active,
                "task": self._task_description,
                "step": self._current_step,
                "max_steps": self._max_steps,
                "state": sm.name,
                "manual_control_active": monitor.is_takeover_active,
                "last_observation": self._last_observation,
                "history_length": len(self._history),
            }

    def start_task(self, task: str, max_steps: int = 15) -> str:
        """Start an autonomous task. Runs in a dedicated background worker."""
        task = (task or "").strip()
        if not task:
            return outcome.failed("screen_agent", "No task instruction was provided")

        with self._lock:
            if self._active:
                return outcome.unverified(
                    "screen_agent",
                    f"A screen task is already active: '{self._task_description}'. "
                    "Cancel it first before starting another.",
                    current_task=self._task_description,
                )

            self._active = True
            self._task_description = task
            self._current_step = 0
            self._max_steps = max(1, min(max_steps, 25))
            self._history.clear()
            self._last_observation = "Task initialized"

        # Initialize manual takeover monitor
        monitor = get_takeover_monitor()
        monitor.start_session(task)

        # Transition state machine
        sm = get_state_machine()
        sm.transition(AssistantState.EXECUTING, reason=f"Started desktop task: {task}")

        # Launch background loop
        self._worker_thread = threading.Thread(
            target=self._run_loop,
            name="ScreenAgentWorker",
            daemon=True,
        )
        self._worker_thread.start()

        return outcome.started(
            "screen_agent",
            task=task,
            max_steps=self._max_steps,
            status="Taking control of screen to complete task. You can move the mouse or say 'Stop' at any time to take back control.",
        )

    def cancel_task(self, reason: str = "User cancelled screen task") -> str:
        with self._lock:
            if not self._active:
                return outcome.done("screen_agent", message="No active screen task to cancel")
            self._active = False

        monitor = get_takeover_monitor()
        monitor.end_session()

        sm = get_state_machine()
        sm.transition(AssistantState.STOPPED, reason=reason)
        return outcome.done("screen_agent", action="cancel", reason=reason, verified=True)

    def resume_task(self) -> str:
        with self._lock:
            if not self._active:
                return outcome.failed("screen_agent", "No active screen task to resume")

        monitor = get_takeover_monitor()
        resumed = monitor.resume_from_takeover()
        if not resumed:
            return outcome.failed("screen_agent", "Could not resume desktop session")

        # Re-trigger worker loop if stopped
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._worker_thread = threading.Thread(
                target=self._run_loop,
                name="ScreenAgentWorker",
                daemon=True,
            )
            self._worker_thread.start()

        return outcome.done("screen_agent", action="resume", task=self._task_description, verified=True)

    def _run_loop(self) -> None:
        """Controlled perception-action-verification loop."""
        print(f"[ScreenAgent] 🚀 Beginning perception-action loop for: '{self._task_description}'")
        monitor = get_takeover_monitor()
        estop = get_emergency_stop()
        sm = get_state_machine()

        while self._current_step < self._max_steps:
            # 1. Safety Checks
            if not self._active or estop.is_cancelled:
                print("[ScreenAgent] 🛑 Task cancelled or emergency stopped.")
                break

            if monitor.is_takeover_active or sm.is_paused:
                print("[ScreenAgent] ⏸️ Paused for manual control. Awaiting user command...")
                time.sleep(1.0)
                continue

            self._current_step += 1
            print(f"[ScreenAgent] ▶ Step {self._current_step}/{self._max_steps}")

            # 2. Perception: Capture Current Screen State
            sm.transition(AssistantState.VERIFYING, reason=f"Capturing screen for step {self._current_step}")
            try:
                img_bytes, mime = _capture_screen()
            except Exception as e:
                print(f"[ScreenAgent] Screen capture failed: {e}")
                break

            # 3. Decision / Planning Step: Ask Gemini what action to take next
            sm.transition(AssistantState.THINKING, reason=f"Analyzing screen for step {self._current_step}")
            action_plan = self._analyze_screen(img_bytes, mime)
            if not action_plan:
                print("[ScreenAgent] ⚠️ Could not determine next action confidently.")
                break

            if action_plan.get("status") == "COMPLETED":
                print(f"[ScreenAgent] ✅ Task completed successfully: {action_plan.get('summary', 'Done')}")
                self._last_observation = action_plan.get("summary", "Task completed")
                break

            if action_plan.get("status") == "UNCERTAIN":
                print(f"[ScreenAgent] ❓ Target uncertain: {action_plan.get('question')}")
                self._last_observation = action_plan.get("question", "Needs clarification")
                break

            # 4. Action Step: Bounded Action Execution
            sm.transition(AssistantState.EXECUTING, reason=f"Executing action step {self._current_step}")
            action_type = action_plan.get("action")
            target_x = action_plan.get("x")
            target_y = action_plan.get("y")
            text_input = action_plan.get("text", "")
            key_input = action_plan.get("key", "")

            # Notify takeover monitor of injected coordinates/keys so it doesn't false-trigger
            if target_x is not None and target_y is not None:
                monitor.notify_injected_mouse(target_x, target_y)
            if text_input or key_input:
                monitor.notify_injected_keyboard()

            exec_result = self._execute_action(action_type, target_x, target_y, text_input, key_input)
            self._history.append({
                "step": self._current_step,
                "action": action_type,
                "result": exec_result,
                "timestamp": time.time(),
            })

            # 5. Wait for application to settle
            time.sleep(1.2)

        # Cleanup at end of session
        with self._lock:
            self._active = False
        monitor.end_session()
        sm.transition(AssistantState.LISTENING, reason="Screen agent finished execution")
        print("[ScreenAgent] 🏁 Perception-action loop concluded.")

    def _analyze_screen(self, img_bytes: bytes, mime: str) -> Optional[dict]:
        """Send screenshot to Gemini to plan the next bounded action."""
        try:
            from google.genai import types as gtypes

            prompt = (
                f"You are the visual cortex of JARVIS desktop assistant.\n"
                f"CURRENT GOAL: {self._task_description}\n"
                f"STEP: {self._current_step} of {self._max_steps}\n"
                f"PREVIOUS ACTIONS: {json.dumps(self._history[-3:]) if self._history else 'None'}\n\n"
                f"Analyze this screenshot and reply with JSON ONLY containing:\n"
                f"{{\n"
                f'  "status": "CONTINUE" | "COMPLETED" | "UNCERTAIN",\n'
                f'  "action": "click" | "double_click" | "type" | "hotkey" | "press" | "wait",\n'
                f'  "x": int,\n'
                f'  "y": int,\n'
                f'  "text": string,\n'
                f'  "key": string,\n'
                f'  "reason": string,\n'
                f'  "summary": string (if completed),\n'
                f'  "question": string (if uncertain)\n'
                f"}}\n"
                f"If the goal has clearly been achieved, set status to COMPLETED.\n"
                f"If you cannot confidently identify the target element, set status to UNCERTAIN.\n"
                f"Do not guess coordinates blindly."
            )

            parts = [
                gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
                prompt,
            ]
            resp = gemini.call(parts, tier=gemini.FAST, timeout_ms=25_000)
            if not resp or not resp.text:
                return None

            raw_text = resp.text.strip()
            # Extract JSON block
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception as e:
            print(f"[ScreenAgent] Vision reasoning error: {e}")
        return None

    def _execute_action(
        self,
        action: str,
        x: Optional[int],
        y: Optional[int],
        text: str,
        key: str,
    ) -> str:
        """Perform a single bounded action via computer_control."""
        try:
            if action in ("click", "left_click"):
                return computer_control.computer_control({"action": "click", "x": x, "y": y})
            if action == "double_click":
                return computer_control.computer_control({"action": "double_click", "x": x, "y": y})
            if action == "type":
                return computer_control.computer_control({"action": "smart_type", "text": text})
            if action == "press":
                return computer_control.computer_control({"action": "press", "key": key or "enter"})
            if action == "hotkey":
                return computer_control.computer_control({"action": "hotkey", "keys": text or key})
            if action == "wait":
                time.sleep(1.0)
                return "Waited 1s"
        except Exception as e:
            return f"Action error: {e}"
        return f"Unknown action '{action}'"


# Global session instance
_GLOBAL_SCREEN_AGENT: Optional[ScreenAgentSession] = None
_SCREEN_AGENT_LOCK = threading.Lock()


def get_screen_agent() -> ScreenAgentSession:
    global _GLOBAL_SCREEN_AGENT
    with _SCREEN_AGENT_LOCK:
        if _GLOBAL_SCREEN_AGENT is None:
            _GLOBAL_SCREEN_AGENT = ScreenAgentSession()
        return _GLOBAL_SCREEN_AGENT


def screen_agent_handler(parameters: dict, **kwargs) -> str:
    """Action handler called by action_loader.py."""
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    agent = get_screen_agent()

    if action in ("take_control", "start", "start_task"):
        task = params.get("task") or params.get("text", "")
        max_steps = int(params.get("max_steps", 15))
        return agent.start_task(task, max_steps=max_steps)

    if action in ("resume", "continue"):
        return agent.resume_task()

    if action in ("cancel", "stop", "abort"):
        return agent.cancel_task(reason=params.get("reason", "Cancelled by user"))

    if action in ("status", "get_status"):
        st = agent.get_status()
        return f"Screen Agent Status: active={st['active']} step={st['step']}/{st['max_steps']} state={st['state']} manual_takeover={st['manual_control_active']}"

    return outcome.failed("screen_agent", f"Unknown screen_agent action: '{action}'")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "screen_agent",
    "description": (
        "Autonomous screen and desktop control: takes control of the screen to "
        "complete multi-step tasks across applications, browsers, windows, and "
        "system settings using a perception-action observation loop. "
        "Use action='start_task' with 'task' when the user says: "
        "'take control of my screen and do X', 'complete this task on my computer'. "
        "Features immediate manual takeover detection: if the user moves their "
        "physical mouse or types, automation pauses immediately. "
        "Other actions: 'resume', 'cancel', 'status'."
    ),
    "brief": "Autonomous desktop control: take control of screen to complete multi-step tasks with manual takeover safety.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "start_task | resume | cancel | status",
            },
            "task": {
                "type": "STRING",
                "description": "Full description of the desktop task to perform.",
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Maximum perception-action steps allowed (default: 15).",
            },
            "reason": {
                "type": "STRING",
                "description": "Reason for cancellation if applicable.",
            },
        },
        "required": ["action"],
    },
    "handler": screen_agent_handler,
}
