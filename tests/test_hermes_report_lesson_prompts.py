import unittest

from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.managers.risk_manager import create_risk_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.trader.trader import create_trader


INSTRUCTION = (
    "Treat report-level lessons as evidence-bounded historical hypotheses. Assess whether each lesson "
    "applies to the current market context, explain any mismatch, and do not let historical outcomes "
    "override current evidence or mechanically force BUY, SELL, or HOLD."
)


class CapturingLlm:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages)
        return type("Response", (), {"content": self.content})()


class StaticMemory:
    def __init__(self, lessons):
        self.lessons = lessons

    def get_memories(self, _situation, n_matches=2):
        return [{"recommendation": lesson} for lesson in self.lessons]


def trader_state():
    return {
        "company_of_interest": "BTC",
        "investment_plan": "Current plan",
        "market_report": "Market",
        "sentiment_report": "Sentiment",
        "news_report": "News",
        "fundamentals_report": "Fundamentals",
    }


def risk_manager_state():
    return {
        "company_of_interest": "BTC",
        "risk_debate_state": {
            "history": "Debate",
            "risky_history": "",
            "safe_history": "",
            "neutral_history": "",
            "current_risky_response": "",
            "current_safe_response": "",
            "current_neutral_response": "",
            "latest_speaker": "",
            "count": 0,
        },
        "market_report": "Market",
        "news_report": "News",
        "sentiment_report": "Sentiment",
        "investment_plan": "Plan",
    }


class HermesReportLessonPromptTests(unittest.TestCase):
    def test_trader_requires_applicability_check_without_forcing_action(self):
        llm = CapturingLlm("FINAL TRANSACTION PROPOSAL: **HOLD**")
        create_trader(llm, StaticMemory(["BTC report lesson."]))(
            trader_state(), "Trader"
        )
        prompt = llm.calls[0][0]["content"]
        self.assertIn("assess whether each historical lesson applies", prompt)
        self.assertIn("must not override current evidence", prompt)

    def test_risk_manager_treats_report_lessons_as_hypotheses(self):
        llm = CapturingLlm("HOLD")
        create_risk_manager(llm, StaticMemory(["BTC report lesson."]))(
            risk_manager_state()
        )
        self.assertIn("evidence-bounded hypotheses", llm.calls[0])

    def test_research_agents_and_manager_receive_report_lesson_instruction(self):
        memory = StaticMemory(["BTC report lesson."])
        states = [
            (create_bull_researcher, {
                "investment_debate_state": {"history": "", "bull_history": "", "current_response": "", "count": 0},
                "market_report": "Market", "sentiment_report": "Sentiment", "news_report": "News", "fundamentals_report": "Fundamentals",
            }),
            (create_bear_researcher, {
                "investment_debate_state": {"history": "", "bear_history": "", "current_response": "", "count": 0},
                "market_report": "Market", "sentiment_report": "Sentiment", "news_report": "News", "fundamentals_report": "Fundamentals",
            }),
            (create_research_manager, {
                "investment_debate_state": {"history": "", "bear_history": "", "bull_history": "", "count": 0},
                "market_report": "Market", "sentiment_report": "Sentiment", "news_report": "News", "fundamentals_report": "Fundamentals",
            }),
        ]
        for factory, state in states:
            with self.subTest(factory=factory.__name__):
                llm = CapturingLlm("HOLD")
                factory(llm, memory)(state)
                self.assertIn(INSTRUCTION, llm.calls[0])


if __name__ == "__main__":
    unittest.main()
