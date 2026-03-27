import orjson as json
from pydantic import BaseModel

from ..constants import MODEL_HEADER_KEY, Model, build_model_header
from ..utils import get_nested_value


class AvailableModel(BaseModel):
    """Model resolved dynamically from Gemini account capabilities."""

    model_id: str
    model_name: str
    display_name: str
    description: str
    capacity: int
    capacity_field: int = 12
    is_available: bool = True

    def __str__(self) -> str:
        return self.model_name or self.display_name

    def __repr__(self) -> str:
        return (
            f"AvailableModel(model_id={self.model_id!r}, "
            f"model_name={self.model_name!r}, description={self.description!r})"
        )

    @property
    def model_header(self) -> dict[str, str]:
        if self.capacity_field == 13:
            tail = f"null,{self.capacity}"
        else:
            tail = str(self.capacity)
        return build_model_header(self.model_id, tail)

    @property
    def advanced_only(self) -> bool:
        return not (self.capacity == 1 and self.capacity_field == 12)

    @staticmethod
    def compute_capacity(tier_flags: list, capability_flags: list) -> tuple[int, int]:
        if 21 in tier_flags:
            return 1, 13
        if 22 in tier_flags:
            return 2, 13
        if 115 in capability_flags:
            return 4, 12
        if 16 in tier_flags or 106 in capability_flags:
            return 3, 12
        if 8 in tier_flags or (106 not in capability_flags and 19 in capability_flags):
            return 2, 12
        return 1, 12

    @staticmethod
    def build_model_id_name_mapping() -> dict[str, str]:
        result: dict[str, str] = {}
        for member in Model:
            if member is Model.UNSPECIFIED:
                continue

            header_value = member.model_header.get(MODEL_HEADER_KEY, "")
            if not header_value:
                continue

            try:
                parsed = json.loads(header_value)
                model_id = get_nested_value(parsed, [4])
            except json.JSONDecodeError:
                continue

            if model_id and model_id not in result:
                base_name = member.name
                if "_" in base_name:
                    base_key = "BASIC_" + base_name.split("_", 1)[-1]
                    base_member = getattr(Model, base_key, member)
                else:
                    base_member = member
                result[model_id] = base_member.model_name

        return result
