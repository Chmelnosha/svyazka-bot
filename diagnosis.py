"""Transparent heuristic routing, not a validated psychological assessment."""
from dataclasses import dataclass
from typing import Optional

# Answer indices are zero based. None means not observed, NOT a failed stage.
WEIGHTS = (
    (0, 1, 0, 2),
    (0, 0, 1, None),
    (0, 1, 1, 2, None),
    (0, 1, 1, 2),
    (0, 1.5, 1.5, None, 2),
    (0, 0.5, 1, 1),
    (0, 1, 2, None),
    (0, 0.5, 1.5, None),
    (0, 1, 1.5, None),
)
BLOCKS = ("traffic", "landing", "sales")
THRESHOLD = 0.65


@dataclass(frozen=True)
class Diagnosis:
    key: str
    secondary: Optional[str]
    evidence: tuple[int, ...]
    scores: tuple[Optional[float], ...]
    coverage: tuple[int, ...]
    limited_data: bool


def diagnose(answers: list[int]) -> Diagnosis:
    if len(answers) != 9:
        raise ValueError("Для результата нужны 9 ответов")
    if any(type(a) is not int or not 0 <= a < len(WEIGHTS[i]) for i, a in enumerate(answers)):
        raise ValueError("Недопустимый ответ")
    values = [WEIGHTS[i][a] for i, a in enumerate(answers)]
    if (answers[0] == 2 or answers[1] == 1) and answers[2] == 3:
        values[2] = 3
    if answers[6] == 3:
        values[6:] = [None, None, None]
    scores, coverage = [], []
    for i in range(0, 9, 3):
        observed = [v for v in values[i:i + 3] if v is not None]
        coverage.append(len(observed))
        scores.append(sum(observed) / len(observed) if observed else None)
    if answers[0] == 3:
        scores[0] = max(scores[0] or 0, 1.5)
    if answers[3] == 3 or answers[4] == 4:
        scores[1] = max(scores[1] or 0, 1.5)
    limited = any(n < 2 for n in coverage)
    if answers[0] == 3 and (answers[3] == 3 or answers[4] == 4) and answers[6] == 3:
        return Diagnosis("unbuilt", None, (0, 4 if answers[4] == 4 else 3), tuple(scores), tuple(coverage), True)
    weak = [i for i, score in enumerate(scores) if score is not None and score >= THRESHOLD]
    weak.sort(key=lambda i: (-scores[i], i))
    if answers[0] == 3 and answers[6] == 3 and 0 in weak:
        weak.remove(0)
        weak.insert(0, 0)
    if not weak:
        return Diagnosis("check", None, (), tuple(scores), tuple(coverage), limited)
    primary = weak[0]
    candidates = [i for i in range(primary * 3, primary * 3 + 3) if values[i] is not None and values[i] > 0]
    candidates.sort(key=lambda i: (-values[i], i))
    return Diagnosis(BLOCKS[primary], BLOCKS[weak[1]] if len(weak) > 1 else None,
                     tuple(candidates[:1]), tuple(scores), tuple(coverage), limited)
