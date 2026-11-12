"""MySQL limits an index to 3072 bytes; utf8mb4 counts 4 bytes a character. v01.00.12 stopped on the active node
(2026-11-12) because a unique CharField of 1024 characters needed 4096. Every indexed text column must fit."""

from django.apps import apps
from django.db import models
from django.test import SimpleTestCase

LIMIT = 3072 // 4  # characters in one indexed utf8mb4 column


class IndexedTextFits(SimpleTestCase):
    def test_every_indexed_char_field_fits_mysql(self):
        too_long = []
        for m in apps.get_models():
            for f in m._meta.concrete_fields:
                if isinstance(f, models.CharField) and (f.unique or f.db_index) and (f.max_length or 0) > LIMIT:
                    too_long.append(f"{m._meta.label}.{f.name} ({f.max_length})")
            for group in list(m._meta.unique_together) + [
                list(c.fields) for c in m._meta.constraints if hasattr(c, "fields")
            ]:
                total = sum(
                    (m._meta.get_field(n).max_length or 0)
                    for n in group
                    if isinstance(m._meta.get_field(n), models.CharField)
                )
                if total > LIMIT:
                    too_long.append(f"{m._meta.label} {tuple(group)} ({total})")
        self.assertEqual(too_long, [])
