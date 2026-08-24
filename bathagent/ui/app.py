"""Streamlit Web Application for BathAgent.
Provides an interactive Customer Service & FinOps portal showcasing Google ADK,
MCP Toolbox for Databases, and Wildfire Proxy integration.
"""

import json
import os
import streamlit as st

from bathagent.agent import BathAgent
from bathagent.config import settings
from bathagent.database.init_db import init_database

# Page Configuration
st.set_page_config(
    page_title="BathAgent - Operations & Customer Service",
    page_icon="🛁",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS for styling
st.markdown(
    """
    <style>
    .main-header {
        font-size: 2.2rem;
        font-weight: 700;
        color: #1A73E8;
        margin-bottom: 0.2rem;
    }
    .sub-header {
        font-size: 1.1rem;
        color: #5F6368;
        margin-bottom: 1.5rem;
    }
    .badge-toolbox {
        background-color: #E8F0FE;
        color: #1A73E8;
        padding: 4px 10px;
        border-radius: 12px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .badge-wildfire {
        background-color: #FCE8E6;
        color: #C5221F;
        padding: 4px 10px;
        border-radius: 12px;
        font-size: 0.85rem;
        font-weight: 600;
    }
    .stButton>button {
        width: 100%;
        border-radius: 8px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_resource
def get_agent():
    """Initializes and caches the BathAgent instance."""
    return BathAgent()


agent = get_agent()

# Sidebar: Configuration and Scenarios
with st.sidebar:
    st.image("https://fonts.gstatic.com/s/i/short-term/release/googlestore/smart_toy/default/24px.svg", width=48)
    st.markdown("### 🛁 BathStuff Control Hub")
    st.markdown("AI Operations & FinOps Assistant powered by **Google ADK**.")

    st.divider()

    st.markdown("#### 🔌 System Integrations")
    
    # Check services
    wf_status = "🟢 Connected" if agent.wildfire.is_server_reachable() else "🟡 Standalone Mode"
    st.markdown(f"**Wildfire Proxy (:8787)**: `{wf_status}`")
    st.markdown(f"**MCP Toolbox**: `🟢 Active (Read/Canned Tools)`")
    st.markdown(f"**Database**: `PostgreSQL / AlloyDB`")

    st.divider()

    st.markdown("#### 🎯 Demo Scenarios")
    st.markdown("Click any scenario to test the agent workflow:")

    if st.button("🔍 1. Alice Smith's Orders", help="Tests MCP Toolbox order lookup"):
        st.session_state.pending_prompt = "Look up all recent orders for customer Alice Smith (customer_id: 101)."

    if st.button("❌ 2. Cancel Unshipped Order", help="Tests MCP Toolbox order cancellation"):
        st.session_state.pending_prompt = "Please cancel order #1001 for customer Alice Smith."

    if st.button("📊 3. Top Selling Toothpastes", help="Tests MCP Toolbox read-only SQL analytics"):
        st.session_state.pending_prompt = "What are our top 3 best-selling toothpastes by quantity?"

    if st.button("🛡️ 4. Toothpaste Tariff Remediation", help="Tests Wildfire Proxy propose_sql & sandbox validation"):
        st.session_state.pending_prompt = (
            "Identify every product imported from Elbonia which is subject to the new tariff on toothpaste. "
            "Then, find all customer orders shipped after July 1, 2026, that contain those products. "
            "For each matching item, append a new line item for the 20% tariff charge with an explanatory note, "
            "and append a corresponding credit line item to offset the charge so the order total remains the same."
        )

    st.divider()
    if st.button("🔄 Reset Demo Database"):
        init_database(use_sqlite=settings.USE_SQLITE)
        st.success("Database reset to pristine baseline!")
        st.rerun()


# Main Application Area
st.markdown('<div class="main-header">🛁 BathAgent Operations Portal</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">Demonstrating safe database operations with <b>Google ADK</b>, <b>MCP Toolbox for Databases</b>, and <b>Wildfire Proxy</b>.</div>',
    unsafe_allow_html=True,
)

# Initialize Chat History
if "messages" not in st.session_state:
    st.session_state.messages = [
        {
            "role": "assistant",
            "content": (
                "Hello! I am **BathAgent**, the AI assistant for BathStuff. "
                "I can assist customer service agents with order management, run analytical lookups via MCP Toolbox, "
                "or safely submit complex database adjustments through the Wildfire Proxy. "
                "How can I help you today?"
            ),
            "tool_calls": [],
        }
    ]

# Process Pending Scenario Click
if "pending_prompt" in st.session_state and st.session_state.pending_prompt:
    prompt_to_send = st.session_state.pending_prompt
    st.session_state.pending_prompt = None
    
    # Append User Message
    st.session_state.messages.append({"role": "user", "content": prompt_to_send})
    
    # Generate Agent Response
    with st.spinner("BathAgent is evaluating tools and processing request..."):
        result = agent.run_prompt(prompt_to_send)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": result.get("text", ""),
                "tool_calls": result.get("tool_calls", []),
            }
        )
    st.rerun()

# Render Chat Messages
for msg in st.session_state.messages:
    if msg["role"] == "user":
        with st.chat_message("user", avatar="👤"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🛁"):
            st.markdown(msg["content"])

            # Render tool execution details if available
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                with st.expander(f"🛠️ Tool Invocation Inspector ({len(tool_calls)} tools used)", expanded=False):
                    for tc in tool_calls:
                        tool_name = tc.get("tool") or tc.get("name", "Tool")
                        badge_class = "badge-wildfire" if "wildfire" in str(tool_name).lower() else "badge-toolbox"
                        st.markdown(f'<span class="{badge_class}">{tool_name}</span>', unsafe_allow_html=True)
                        st.json(tc)

# Chat Input Box
user_prompt = st.chat_input("Ask BathAgent a question or give an operational command...")
if user_prompt:
    st.session_state.messages.append({"role": "user", "content": user_prompt})
    with st.spinner("BathAgent is evaluating tools and processing request..."):
        result = agent.run_prompt(user_prompt)
        st.session_state.messages.append(
            {
                "role": "assistant",
                "content": result.get("text", ""),
                "tool_calls": result.get("tool_calls", []),
            }
        )
    st.rerun()
