import re
import logging
from difflib import SequenceMatcher
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger("TaskResolver")

# In-memory context cache for resolving ambiguous follow-up requests (e.g., "done")
chat_contexts: Dict[str, str] = {}


def execute_completion(matched_tasks: List[Dict[str, Any]], all_pending: List[Dict[str, Any]]) -> str:
    """Marks matched task(s) completed in Google Tasks and returns an encouraging response."""
    from google_tasks_service import tasks_service

    completed_titles = []
    errors = []
    matched_ids = {t["id"] for t in matched_tasks}

    for t in matched_tasks:
        try:
            tasks_service.complete_task(t["id"])
            completed_titles.append(t["title"])
        except Exception as e:
            logger.error(f"Error completing task '{t.get('title')}': {e}")
            errors.append(f"{t.get('title')} ({e})")

    remaining_tasks = [t for t in all_pending if t["id"] not in matched_ids]
    remaining = len(remaining_tasks)

    if len(completed_titles) == 1:
        res = f"✅ Completed: *{completed_titles[0]}*!\nStreak preserved! 🔥 You have *{remaining} pending task(s)* left."
        if remaining_tasks:
            res += f"\nNext up: 📌 *{remaining_tasks[0]['title']}*"
        else:
            res += "\n🎉 Amazing work! All your tasks are cleared for today!"
    else:
        bullets = "\n".join([f"• 📌 *{title}*" for title in completed_titles])
        res = (
            f"🎉 Awesome! Completed *{len(completed_titles)} tasks*:\n{bullets}\n\n"
            f"Streak preserved! 🔥 You have *{remaining} pending task(s)* left."
        )
        if remaining_tasks:
            res += f"\nNext up: 📌 *{remaining_tasks[0]['title']}*"
        else:
            res += "\n🎉 Incredible streak! All tasks completed!"

    if errors:
        res += f"\n⚠️ Warning: could not complete {', '.join(errors)}"
    return res


COMPLETION_PATTERNS = [
    r'\bdone\b', r'\bcompleted?\b', r'\bfinished?\b',
    r'\bho\s*gay[aa]\b', r'\bkhatam\b', r'\bcheck(?:ed)?\b',
    r'\bcleared?\b', r'\bsolved?\b', r'\bmarked?\b'
]


def resolve_tasks_to_complete(
    user_message: str,
    pending_tasks: List[Dict[str, Any]],
    last_assistant_message: Optional[str] = None
) -> Tuple[List[Dict[str, Any]], str]:
    """
    Intelligently determines which pending task(s) the user wants to mark completed.
    
    Supports:
    1. "done all" / "clear all" -> all tasks
    2. "done all <keyword>" / "complete all aws" -> all tasks matching keyword
    3. Number/index: "done 3", "complete #2", "task 13 done"
    4. Task title/keyword: "done daily sign", "finished oracle exam", "completed claude code"
    5. Contextual "done": if user previously discussed a specific task (e.g. AWS Daily Streak),
       replying "done" completes that specific task instead of blindly completing #1.
    6. Default fallback: pure "done" with no context completes the top active task.
    """
    if not pending_tasks:
        return [], "no_tasks"

    msg = user_message.strip()
    norm = msg.lower()

    has_completion_verb = any(re.search(p, norm) for p in COMPLETION_PATTERNS)

    # 1. "done all" / "complete all"
    if re.search(r'\b(?:done|complete|completed|finish|finished|clear|cleared)\s+all\s*$', norm):
        return pending_tasks, "all_tasks"

    # 2. "done all <keyword>" / "complete all aws"
    all_kw_match = re.search(
        r'\b(?:done|complete|completed|finish|finished|clear|cleared)\s+(?:all|every)\s+([a-z0-9_\-\s]+)',
        norm
    )
    if all_kw_match:
        target_kw = all_kw_match.group(1).strip()
        matched = [
            t for t in pending_tasks
            if target_kw in t["title"].lower() or any(
                len(w) > 2 and w in t["title"].lower() for w in target_kw.split()
            )
        ]
        if matched:
            return matched, f"all_keyword_{target_kw}"

    # 3. Number based: "done 13", "done #13", "task 2 done", "#4 completed", "done 3"
    num_match = re.search(r'(?:done|complete|completed|finish|finished|task|#)\s*#?(\d+)\b', norm)
    if not num_match:
        num_match = re.search(r'^#?(\d+)\s*(?:done|completed|finished)?$', norm)
    if num_match:
        idx = int(num_match.group(1)) - 1
        if 0 <= idx < len(pending_tasks):
            return [pending_tasks[idx]], f"index_{idx+1}"

    # 4. Keyword / Title match when a completion verb is present
    if has_completion_verb:
        # Strip common prefixes like "i have done", "done with", "marked"
        cleaned = re.sub(
            r'^(?:i(?:\'ve|\s+have)?\s+)?(?:just\s+)?(?:done|completed?|finished?|clear|cleared|check|checked|ho\s*gay[aa]|khatam)\s*(?:with|all)?\s*',
            '', norm
        ).strip()
        # Strip trailing completion verbs
        cleaned = re.sub(
            r'\s*(?:is|are)?\s*(?:done|completed?|finished?|ho\s*gay[aa]|khatam|checked)$',
            '', cleaned
        ).strip()
        # Strip filler words
        cleaned = re.sub(r'^(?:my|the|task|streak)\s+', '', cleaned).strip()

        if cleaned:
            # A. Direct substring match
            direct = [t for t in pending_tasks if cleaned in t["title"].lower()]
            if direct:
                return [direct[0]], f"substring_{cleaned}"

            # B. Token matching (e.g. "daily sign" -> "AWS Daily Streak: Sign-in...")
            tokens = [w for w in re.split(r'[\s\-:,]+', cleaned) if len(w) > 2]
            if tokens:
                # All tokens match
                token_matches = [
                    t for t in pending_tasks
                    if all(tok in t["title"].lower() for tok in tokens)
                ]
                if token_matches:
                    return [token_matches[0]], f"tokens_{tokens}"

                # Partial token overlap / fuzzy similarity
                scored = []
                for t in pending_tasks:
                    title_lower = t["title"].lower()
                    overlap = sum(1 for tok in tokens if tok in title_lower)
                    sim = SequenceMatcher(None, cleaned, title_lower).ratio()
                    if overlap > 0 or sim >= 0.4:
                        scored.append((overlap, sim, t))
                if scored:
                    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    return [scored[0][2]], f"token_sim_{scored[0][2]['title']}"

    # 5. Pure "done" / "complete" without specific target
    pure_completion = norm in [
        "done", "done!", "complete", "completed", "finish", "finished",
        "ho gaya", "khatam", "check", "ho gya", "done with it", "finished it"
    ]
    if pure_completion or has_completion_verb:
        # A. Check context: Did the last assistant message mention a specific pending task?
        if last_assistant_message:
            best_match = None
            best_score = 0
            for t in pending_tasks:
                words = [w for w in re.split(r'[\s\-:,]+', t["title"].lower()) if len(w) > 2]
                if words:
                    matches = sum(1 for w in words if w in last_assistant_message.lower())
                    if matches >= 2 and matches > best_score:
                        best_score = matches
                        best_match = t
            if best_match:
                return [best_match], f"context_from_last_message_{best_match['title']}"

        # B. Fallback to top task if pure completion
        if pure_completion:
            return [pending_tasks[0]], "top_task_fallback"

    return [], "no_completion"

