import sys
sys.path.insert(0, ".")
import harness

agent = harness.build([])
for tool in agent._tools.registry.list():
    params = ", ".join(f"{n}{'' if p.required else '?'}:{getattr(p, 'type', '')}" for n, p in tool.parameters.items() if not p.hidden)
    print(f"{tool.name}({params})")
harness.CONTROL["stop"]()
