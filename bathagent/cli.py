"""Interactive CLI interface for BathAgent."""

import sys
from bathagent.agent import BathAgent
from bathagent.config import settings


def main():
    print("=" * 70)
    print(" 🛁  BathAgent - BathStuff Operations AI Assistant (ADK)")
    print("=" * 70)
    print("Connected to:")
    print(f" • Database: {settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME} (SQLite fallback: {settings.USE_SQLITE})")
    print(f" • MCP Toolbox: {settings.TOOLBOX_URL}")
    print(f" • Wildfire Proxy: {settings.WILDFIRE_URL}")
    print("=" * 70)
    print("Type your message below, or try one of these demo prompts:")
    print(" 1. 'Look up orders for Alice Smith (customer_id: 101)'")
    print(" 2. 'Cancel order #1001 for customer Alice Smith'")
    print(" 3. 'What are our top 3 best-selling toothpastes?'")
    print(" 4. 'Run retroactive tariff remediation for Elbonian toothpaste orders shipped post-July 1'")
    print("Type 'exit' or 'quit' to stop.\n")

    agent = BathAgent()

    while True:
        try:
            user_input = input("\n👤 You > ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("\nGoodbye!")
                break

            print("\n🤖 BathAgent is thinking...\n")
            result = agent.run_prompt(user_input)

            # Print tool calls if any
            tool_calls = result.get("tool_calls", [])
            if tool_calls:
                print("🔧 [Tool Executions]:")
                for tc in tool_calls:
                    tool_name = tc.get("tool") or tc.get("name")
                    print(f"   ↳ {tool_name}")
                print()

            print(result.get("text", ""))

        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
