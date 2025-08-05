import uuid
import logging
from typing import List, Dict, Any, Optional
from utils.fireworkzz import get_fireworks_response
from utils.s3_utils import read_json_from_s3
from collections import defaultdict


class WorkflowRunner:
    def __init__(self, userid: str, filename: str):
        self.userid = userid
        self.filename = filename
        self.wf_loc = f"{userid}/workflow/{filename}"
        self.workflow_json = read_json_from_s3(self.wf_loc)
        self.maxRetries = 2
        self.workflow = self.workflow_json.get("workflow", {})
        self.steps = {step["id"]: step for step in self.workflow.get("steps", [])}
        self.input_data = self.workflow_json.get("input_data", {})
        self.clarification_answers = {
            item["quote"]: item["answer"]
            for item in self.workflow_json.get("clarification_answers", [])
        }
        self.execution_log: List[Dict[str, Any]] = []
        self.logger = logging.getLogger(f"WorkflowRunner-{userid}-{filename}")
        self.logger.setLevel(logging.INFO)
        if (
            not self.logger.handlers
        ):  # Prevent adding multiple handlers in debug mode or multiple runs
            handler = logging.StreamHandler()
            formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

    def execute(self):
        start_step_id = self._get_first_step()
        if not start_step_id:
            self.logger.error("No valid start step found.")
            return

        current_step_id = start_step_id
        visited = defaultdict(int)
        MAX_RETRIES_PER_STEP = self.maxRetries

        while current_step_id:
            visited[current_step_id] += 1

            if visited[current_step_id] > MAX_RETRIES_PER_STEP:
                self.logger.warning(
                    f"Infinite loop detected at step {current_step_id}. Max retries exceeded."
                )
                break

            step = self.steps.get(current_step_id)
            if not step:
                self.logger.error(f"Step ID {current_step_id} not found.")
                break

            try:
                self.logger.info(f"Executing step: {step['title']} [{step['id']}]")
                step_result = self._execute_step(step)
                self.execution_log.append(
                    {
                        "step_id": step["id"],
                        "step_title": step["title"],
                        "result": step_result,
                    }
                )
                current_step_id = step_result.get("next_step")
            except Exception as e:
                self.logger.error(f"Error executing step {step['id']}: {e}")
                fallback = self._find_fallback_for_step(step)
                if fallback:
                    self.logger.warning(
                        f"Switching to fallback step: {fallback['title']} [{fallback['id']}]"
                    )
                    current_step_id = fallback["id"]
                else:
                    self.logger.error(
                        f"No fallback step defined for step {step['id']}. Ending execution."
                    )
                    break

    def _get_first_step(self) -> Optional[str]:
        # First step is usually the one not referenced by any `next_step`
        referenced = set()
        for step in self.steps.values():
            ns = step.get("next_step")
            if isinstance(ns, list):
                referenced.update(ns)
            elif isinstance(ns, str):
                referenced.add(ns)

        for step_id in self.steps:
            if step_id not in referenced:
                return step_id
        return None

    def _execute_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        step_type = step.get("type")

        if step_type == "communication":
            return self._handle_communication(step)
        elif step_type == "navigation":
            return self._handle_navigation(step)
        elif step_type == "self-learn":
            return self._handle_self_learn(step)
        else:
            raise ValueError(f"Unknown step type: {step_type}")

    def _handle_communication(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Simulate sending a message or content via a channel
        ai_output = (
            f"[COMMUNICATION] via {step.get('channels')} - {step['ai_instructions']}"
        )
        self.logger.info(ai_output)

        if step.get("decision_point"):
            return self._handle_decision(step, ai_output)
        else:
            return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_navigation(self, step: Dict[str, Any]) -> Dict[str, Any]:
        # Simulate opening a page
        ai_output = f"[NAVIGATION] Go to {step.get('page_url')}"
        self.logger.info(ai_output)
        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_self_learn(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """
        Handle 'self-learn' type steps. These may involve:
        - Autonomous AI-driven actions using ai_instructions.
        - Decision branching based on AI interpretation.
        - Optional use of clarification answers or querying external sources.
        """

        ai_instruction = step.get("ai_instructions", "")
        user_context = self.user_data or ""
        clarifications = self.clarification_answers or ""

        full_prompt = f"""You are executing an autonomous step in a workflow.

        AI Instruction:
        {ai_instruction}

        User Data:
        {user_context}

        Clarification Answers:
        {clarifications}

        Respond appropriately based on the instruction above.
        """

        # Run AI decision
        ai_output = get_fireworks_response(full_prompt, role="system")

        self.logger.info(f"[SELF-LEARN AI] Instruction: {ai_instruction}")
        self.logger.info(f"[SELF-LEARN AI] Output: {ai_output}")

        # # Optional: fallback to knowledge retrieval if defined
        # if step.get("use_external_docs"):
        #     question = step.get("objective", "") or ai_instruction
        #     kb_response = agent.query_vector(question)
        #     self.logger.info(
        #         f"[SELF-LEARN External Doc] Query: {question} → Response: {kb_response}"
        #     )
        #     ai_output += f"\n\n[Knowledge Base Response]\n{kb_response}"

        # Decision logic if applicable
        if step.get("decision_point"):
            return self._handle_decision(step, ai_output)

        # Default next step
        return {"output": ai_output, "next_step": step.get("next_step")}

    def _handle_decision(self, step: Dict[str, Any], ai_output: str) -> Dict[str, Any]:
        # Placeholder: simulate a decision outcome (first condition as default for now)
        conditions = step.get("condition", [])
        next_steps = step.get("next_step", [])

        # You could plug in actual AI/ML logic here later
        selected_index = 0
        selected_condition = conditions[selected_index] if conditions else "default"
        selected_next_step = next_steps[selected_index] if next_steps else None

        self.logger.info(
            f"[DECISION] Condition '{selected_condition}' matched → {selected_next_step}"
        )
        return {
            "output": f"{ai_output}\nDecision: {selected_condition}",
            "next_step": selected_next_step,
        }

    def _find_fallback_for_step(
        self, original_step: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        # Fallbacks are often separate steps not connected directly
        fallback_candidates = [
            step
            for step in self.steps.values()
            if "fallback" in step.get("title", "").lower()
            or "fallback" in step.get("objective", "").lower()
        ]
        for step in fallback_candidates:
            if step["id"] != original_step["id"]:
                return step
        return None

    def get_execution_log(self) -> List[Dict[str, Any]]:
        return self.execution_log
