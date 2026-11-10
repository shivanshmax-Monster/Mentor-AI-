import logging
from typing import List, Dict, Any, Optional
from google import genai
from google.genai import types

from config import config

logger = logging.getLogger("GeminiService")

# Candidate Flash models in order of quota & responsiveness
CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.8-flash"
]


class GeminiService:
    def __init__(self):
        self._client = None
        self._last_key = None

    def get_client(self) -> genai.Client:
        current_key = config.GEMINI_API_KEY
        if not current_key or current_key.startswith("your_"):
            raise ValueError(
                "GEMINI_API_KEY is not set or is invalid! "
                "Please get a free API key from https://aistudio.google.com and enter it in Settings."
            )

        if self._client is None or self._last_key != current_key:
            self._client = genai.Client(api_key=current_key)
            self._last_key = current_key
        return self._client

    def _extract_text(self, response) -> str:
        """Safely extracts text even if model was stopped or has thinking tokens."""
        if response and getattr(response, "text", None):
            return response.text.strip()

        if response and getattr(response, "candidates", None) and response.candidates:
            content = getattr(response.candidates[0], "content", None)
            if content and getattr(content, "parts", None):
                for part in content.parts:
                    p_text = getattr(part, "text", None)
                    if p_text and p_text.strip():
                        return p_text.strip()

        return "HabitSync AI online!"

    def _generate_with_fallback(self, contents: Any, config_obj: Optional[types.GenerateContentConfig] = None) -> str:
        """Attempts generation using the configured model, and automatically falls back if 429/503 occurs."""
        client = self.get_client()

        models_to_try = [config.GEMINI_MODEL] + [m for m in CANDIDATE_MODELS if m != config.GEMINI_MODEL]

        last_error = None
        for model in models_to_try:
            try:
                logger.info(f"Attempting generation with model: {model}")
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config_obj
                )
                text = self._extract_text(response)
                if text:
                    if model != config.GEMINI_MODEL:
                        logger.info(f"Updating preferred model to {model} after fallback success")
                        config.save_settings({"GEMINI_MODEL": model})
                    return text
            except Exception as e:
                err_str = str(e)
                logger.warning(f"Model {model} failed: {err_str}")
                last_error = e
                # Fall back on 503 high demand, 429 quota exhausted, or 404 not found
                if any(k in err_str for k in ["503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND"]):
                    continue
                else:
                    raise e

        raise last_error

    def test_connection(self) -> str:
        """Sends a quick test ping to Gemini to verify the API key with safe token budget."""
        return self._generate_with_fallback(
            contents="Say 'HabitSync AI online!' in 4 words or less.",
            config_obj=types.GenerateContentConfig(max_output_tokens=300)
        )

    def chat_with_tasks(
        self,
        user_message: str,
        pending_tasks: List[Dict[str, Any]],
        conversation_history: Optional[List[Dict[str, str]]] = None
    ) -> str:
        """
        Sends user message to Gemini with system context including the user's pending tasks.
        Returns a concise, natural, supportive WhatsApp-formatted message.
        """
        if pending_tasks:
            task_lines = [f"- {t['title']} (ID: {t['id']})" for t in pending_tasks]
            tasks_context = "Current pending tasks in Google Tasks:\n" + "\n".join(task_lines)
        else:
            tasks_context = "All tasks are currently completed! There are no pending tasks."

        system_instruction = f"""
You are "HabitSync AI", a personal accountability companion and friendly habit buddy on WhatsApp.
Your job is to help the user stay consistent, finish their daily tasks, preserve their streaks, and overcome procrastination.

Context about the user:
{tasks_context}

Communication Guidelines:
1. Tone: Friendly, motivating, direct, and conversational. Speak like a smart accountability partner (use English or casual Hinglish if the user chats in Hinglish).
2. Brevity: You are texting on WhatsApp! Keep replies concise—around 2 to 4 sentences maximum. Avoid long essay paragraphs or excessive formatting.
3. Relevant Action:
   - If the user asks what tasks are pending, tell them concisely.
   - If the user expresses laziness, procrastination, or lack of motivation, give a quick 2-minute actionable nudge (e.g., the 5-minute rule).
   - If the user asks how to complete a task, remind them they can simply text "done" or tell you they finished it.
4. Emojis: Use 1-2 natural emojis (e.g. 🚀, 📌, 🔥, ✅).
"""

        return self._generate_with_fallback(
            contents=user_message,
            config_obj=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.7,
                max_output_tokens=500
            )
        )


gemini_service = GeminiService()
