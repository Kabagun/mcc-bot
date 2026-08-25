"""Offline MCC descriptions used by the Telegram and CLI output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .catalog import CatalogError, normalize_mcc
from .resources import DEFAULT_DESCRIPTIONS_PATH


@dataclass(frozen=True, slots=True)
class DescriptionCatalog:
    """Validated MCC-to-Russian-label mapping."""

    labels: dict[str, str]

    @classmethod
    def from_file(cls, path: Path | str = DEFAULT_DESCRIPTIONS_PATH) -> DescriptionCatalog:
        path = Path(path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CatalogError(f"Описания {path} должны быть в кодировке UTF-8") from exc
        except OSError as exc:
            raise CatalogError(
                f"Не удалось прочитать описания {path}: {exc.strerror or exc}"  # noqa: RUF001
            ) from exc
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"Описания {path} содержат некорректный JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise CatalogError("Корень описаний MCC должен быть объектом")
        labels: dict[str, str] = {}
        for raw_mcc, raw_label in raw.items():
            try:
                mcc = normalize_mcc(str(raw_mcc))
            except (TypeError, ValueError) as exc:
                raise CatalogError(
                    f"Ключ описаний MCC {raw_mcc!r} должен содержать четыре цифры"
                ) from exc
            if not isinstance(raw_label, str) or not raw_label.strip():
                raise CatalogError(f"Описание MCC {mcc} должно быть непустой строкой")
            labels[mcc] = " ".join(raw_label.split())
        return cls(labels=labels)

    def get(self, raw_mcc: str) -> str:
        """Return a label or the deterministic missing-description text."""

        mcc = normalize_mcc(raw_mcc)
        return self.labels.get(mcc, "описание не найдено")

    def __getitem__(self, raw_mcc: str) -> str:
        return self.get(raw_mcc)

    def __contains__(self, raw_mcc: object) -> bool:
        if not isinstance(raw_mcc, str):
            return False
        try:
            return normalize_mcc(raw_mcc) in self.labels
        except ValueError:
            return False


def load_descriptions(path: Path | str = DEFAULT_DESCRIPTIONS_PATH) -> DescriptionCatalog:
    """Load descriptions while presenting all malformed input as CatalogError."""

    return DescriptionCatalog.from_file(path)
