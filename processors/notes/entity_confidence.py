"""
Entity Resolution Confidence

Turns the evidence behind one resolution into a calibrated probability that it is
correct, so the user only reviews the ones that are genuinely uncertain.

The AI's own stated probability is one input, but on its own it is not calibrated.
It is combined with the registry's evidence (how this spelling was resolved before,
whether the entity is an attendee, how closely the names match, ...) in a logistic
model fitted on the user's past resolutions by ``scripts/entity_resolution_eval.py``,
which also prints how well the probabilities match the observed error rates.
Refit and paste the new weights here when the eval script says calibration has drifted.
"""

import math
from typing import Dict, List, Optional

from .entity_registry import AliasEvidence, Candidate, entity_key

# Kinds of answer the resolver can give for a mention
EXISTING, NEW, NONE = "existing", "new", "none"

FEATURES = [
    "bias",
    "llm_logit",          # logit of the AI's stated probability
    "is_new",
    "is_none",
    "hist_log_total",     # log(1 + times this spelling was resolved before)
    "hist_share",         # share of those that agree with this answer
    "hist_disagrees",     # this spelling was resolved before, never to this answer
    "is_attendee",
    "similarity",
    "token_match",
    "companion_score",
    "log_meetings",       # how established the chosen entity is
    "n_close",            # other candidates that are close spelling matches
]

# Fitted by scripts/entity_resolution_eval.py (see module docstring) on 3,002 mentions
# from 130 meetings replayed on 2026-10-27, 48 of them with Entity Descriptions.
# Out of fold, answers scored >= 0.95 were right 99.2% of the time, against the
# user's past answers.
WEIGHTS: Dict[str, float] = {
    "bias": -0.638,
    "llm_logit": 1.229,
    "is_new": -0.475,
    "is_none": -0.402,
    "hist_log_total": 0.156,
    "hist_share": 0.960,
    "hist_disagrees": -1.279,
    "is_attendee": 0.187,
    "similarity": 0.439,
    "token_match": -0.014,
    "companion_score": 0.118,
    "log_meetings": 0.143,
    "n_close": -0.464,
}


def _logit(p: float) -> float:
    p = min(max(p, 0.01), 0.995)
    return math.log(p / (1 - p))


def answer_kind(link: str, evidence: AliasEvidence) -> str:
    if not link:
        return NONE
    key = entity_key(link)
    return EXISTING if any(c.key == key for c in evidence.candidates) else NEW


def features(link: str, evidence: AliasEvidence, llm_probability: float) -> Dict[str, float]:
    """Feature vector for the answer `link` ('' meaning no link) to one mention."""
    kind = answer_kind(link, evidence)
    key = entity_key(link)
    chosen: Optional[Candidate] = next((c for c in evidence.candidates if c.key == key), None) if key else None

    if kind == NONE:
        agreeing = evidence.rejected
    else:
        agreeing = chosen.alias_count if chosen else 0
    total = evidence.total

    n_close = sum(1 for c in evidence.candidates if c.similarity >= 0.85 and c.key != key)

    return {
        "bias": 1.0,
        "llm_logit": _logit(llm_probability),
        "is_new": float(kind == NEW),
        "is_none": float(kind == NONE),
        "hist_log_total": math.log1p(total),
        "hist_share": agreeing / total if total else 0.0,
        "hist_disagrees": float(total > 0 and agreeing == 0),
        "is_attendee": float(bool(chosen and chosen.is_attendee)),
        "similarity": chosen.similarity if chosen else 0.0,
        "token_match": float(bool(chosen and chosen.token_match)),
        "companion_score": chosen.companion_score if chosen else 0.0,
        "log_meetings": math.log1p(chosen.meetings) if chosen else 0.0,
        "n_close": float(min(n_close, 4)),
    }


def probability(feats: Dict[str, float], weights: Dict[str, float] = WEIGHTS) -> float:
    z = sum(weights.get(name, 0.0) * feats.get(name, 0.0) for name in FEATURES)
    return 1 / (1 + math.exp(-z))


def vector(feats: Dict[str, float]) -> List[float]:
    return [feats[name] for name in FEATURES]
