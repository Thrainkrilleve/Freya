# Freya approved development context
Freya is a local-first, single persistent assistant identity. Models and agents are subordinate interchangeable workers, not independent Freya personas. The Discord ChatGPT and Claude accounts are development assistants, not Freya's production identity.
Architecture: owner-controlled orchestration, explicit approvals for effectful operations, least privilege, audit history, isolation, and private memory. Distributed volunteer compute is experimental and must never receive private memories, credentials, or personal conversations without separately authorized security design.
Human contributors propose changes; only the owner approves consequential decisions. Never push directly to main. Code contributions go through reviewed pull requests and CI.
Project RUO is separate. Its original Discord collector must not be modified or connected to Freya's credentials.
