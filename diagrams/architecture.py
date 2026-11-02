"""Generate editable Foodie AI architecture using mingrammer/diagrams.

Install: brew install graphviz; python -m pip install diagrams
Run from repo root: python diagrams/architecture.py
Reflects the implemented MVP: dashed edges are optional providers that are off by default.
"""
from diagrams import Cluster, Diagram, Edge
from diagrams.generic.compute import Rack
from diagrams.generic.database import SQL
from diagrams.generic.network import Router

with Diagram("Foodie AI local-first architecture", filename="docs/architecture_generated", show=False, direction="TB"):
    chat = Rack("iMessage group")
    with Cluster("Mac / messaging bridge (bot macOS user)"):
        mac = Rack("macOS Messages")
        bridge = Router("BlueBubbles\n(public REST/webhook)")
    with Cluster("Python backend (FastAPI)"):
        api = Rack("Webhook + admission\nallowlist, wake, dedupe")
        engine = Rack("Orchestrator\nversioned sessions")
        ranking = Rack("Filter + ranking\nvoting")
        outbox = Rack("Outbox\nversion re-check")
        db = SQL("SQLite (WAL)")
        rules = Rack("Rules decisions")
        snapshot = Rack("OSM snapshot (offline)")
    with Cluster("External services (optional, configured)"):
        jev = Rack("Jev decision API")
        places = Rack("Nominatim + Overpass")
    chat >> mac >> bridge >> api >> engine
    engine >> db
    engine >> rules
    engine >> snapshot
    engine >> Edge(style="dashed", label="ambiguous only") >> jev
    engine >> Edge(style="dashed") >> places
    engine >> ranking >> outbox >> bridge
