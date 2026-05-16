from typing import Any, Dict


class ContextSummarizer:
    def __init__(self, summarizer_llm: Any, threshold_pct: float = 60.0, keep_recent: int = 6):
        self.llm = summarizer_llm
        self.threshold_pct = threshold_pct
        self.keep_recent = keep_recent
        self.total_summarization_cost = 0.0
        self._summarizations = 0
        self._tokens_saved = 0

    def summarize(self, session: Any, context_window: int, system_tokens: int) -> bool:
        return False

    def stats(self) -> Dict[str, Any]:
        return {
            "tokens_saved": self._tokens_saved,
            "summarization_cost": self.total_summarization_cost,
            "summarizations": self._summarizations,
        }
