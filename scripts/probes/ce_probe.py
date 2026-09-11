"""What does Hindsight's cross-encoder actually see, and what does each piece
of noise cost? Runs INSIDE the hindsight container with its own loaded model."""
import asyncio
import math

from hindsight_api.engine.cross_encoder import LocalSTCrossEncoder

sig = lambda x: 1 / (1 + math.exp(-x))

# The `make smoke` false negative: a 1-fact bank, a query it answers, final=0.000024.
Q = "how are Python dependencies managed here"
F = "This project pins its Python dependencies with uv, never with pip."
DATE = "[Date: September 11, 2026 (2026-09-11)] "
SUFFIX = " (mentioned_at=2026-09-11 10:14:31.026616+00:00)"

# q05 from the clean corpus, alice only
Q5 = "how should Python packages be installed"
U05 = "Alice runs every Python project inside a virtual environment, never system-wide."

# The language rule, quantified
Q_ES = "cómo se gestionan las dependencias de Python aquí"
F_ES = "Este proyecto fija sus dependencias de Python con uv, nunca con pip."

cases = [
    ("smoke: bare fact",                 Q, F),
    ("smoke: + [Date] prefix",           Q, DATE + F),
    ("smoke: + (mentioned_at) suffix",   Q, F + SUFFIX),
    ("smoke: + both",                    Q, DATE + F + SUFFIX),
    ("smoke: query rephrased 'pinned'",  "how are Python dependencies pinned in this project", F),
    ("q05: alice-only, bare",            Q5, U05),
    ("q05: + [Date] prefix",             Q5, DATE + U05),
    ("lang: EN query / EN fact",         Q, F),
    ("lang: ES query / ES fact",         Q_ES, F_ES),
    ("lang: EN query / ES fact",         Q, F_ES),
    ("lang: ES query / EN fact",         Q_ES, F),
]

async def main():
    ce = LocalSTCrossEncoder()
    r = ce.initialize()
    if asyncio.iscoroutine(r):
        await r
    logits = await ce.predict([(q, d) for _, q, d in cases])
    print(f"model: {ce.model_name}\n")
    print(f"{'case':40} {'logit':>8}  {'sigmoid':>10}")
    for (label, _, _), lg in zip(cases, logits):
        print(f"{label:40} {lg:8.3f}  {sig(lg):10.6f}")

asyncio.run(main())
