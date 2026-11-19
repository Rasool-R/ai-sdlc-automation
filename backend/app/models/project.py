"""Project ORM model grouping connected repositories and workflows."""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ProjectModel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Database model representing an engineering project container."""

    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(150), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)