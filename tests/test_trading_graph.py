import unittest
from unittest.mock import Mock

from tradingagents.graph.trading_graph import TradingAgentsGraph


class TradingGraphLoggingTests(unittest.TestCase):
    def make_graph(self, config):
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = config
        graph.debug = False
        graph.propagator = Mock()
        graph.propagator.create_initial_state.return_value = {"initial": "state"}
        graph.propagator.get_graph_args.return_value = {}
        graph.graph = Mock()
        graph.graph.invoke.return_value = {"final_trade_decision": "HOLD"}
        graph.process_signal = Mock(return_value="HOLD")
        graph._log_state = Mock()
        return graph

    def test_propagate_skips_state_logging_when_disabled_in_config(self):
        graph = self.make_graph({"log_graph_states": False})

        final_state, signal = graph.propagate("BTCUSDT", "2026-07-28")

        self.assertEqual(final_state, {"final_trade_decision": "HOLD"})
        self.assertEqual(signal, "HOLD")
        graph._log_state.assert_not_called()

    def test_propagate_logs_state_when_logging_option_is_absent(self):
        graph = self.make_graph({})

        graph.propagate("BTCUSDT", "2026-07-28")

        graph._log_state.assert_called_once_with(
            "2026-07-28", {"final_trade_decision": "HOLD"}
        )


if __name__ == "__main__":
    unittest.main()
