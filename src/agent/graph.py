from langgraph.graph import END, StateGraph
import inspect
from src.agent.state import AgentState


def after_triage(state):
    return "end" if state.get("error") or state.get("payload") else "decide"


def after_decision(state):
    if state.get("error"):
        return "end"
    return "tools" if state["messages"][-1].tool_calls else "validate"


def after_tools(state):
    return "end" if state.get("error") else "decide"


def build_graph(agent=None):
    if agent is None:
        from src.agent.nodes import AnalysisAgent
        agent = AnalysisAgent()
    graph = StateGraph(AgentState)
    def tracked(handler):
        async def node(state):
            agent.remember(state)
            result = handler(state)
            if inspect.isawaitable(result):
                result = await result
            agent.remember(state, result)
            return result
        return node
    graph.add_node("triage", tracked(agent.triage))
    graph.add_node("decide", tracked(agent.decide))
    graph.add_node("tools", tracked(agent.tools))
    graph.add_node("validate", tracked(agent.validate))
    graph.set_entry_point("triage")
    graph.add_conditional_edges("triage", after_triage, {"end": END, "decide": "decide"})
    graph.add_conditional_edges("decide", after_decision, {"end": END, "tools": "tools", "validate": "validate"})
    graph.add_conditional_edges("tools", after_tools, {"end": END, "decide": "decide"})
    graph.add_edge("validate", END)
    return graph.compile()
