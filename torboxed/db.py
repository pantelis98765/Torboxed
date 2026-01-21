from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from torboxed.config import settings


class Base(DeclarativeBase):
    pass


class KVSetting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)  # "torrent" | "nzb"
    category: Mapped[str | None] = mapped_column(String(50), nullable=True)  # "radarr" | "sonarr" | "whisparr" | None

    status: Mapped[str] = mapped_column(
        String(50), default="queued", nullable=False
    )  # queued|submitted|downloading|completed|failed|cancelled
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0-100

    # Approximate current local download speed in bytes/sec (updated while downloading)
    current_speed_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)

    torbox_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    torbox_download_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    # Best-effort migrations for new columns on existing installations
    with engine.begin() as conn:
        try:
            conn.exec_driver_sql("ALTER TABLE downloads ADD COLUMN current_speed_bps INTEGER")
        except Exception:
            # Column already exists or table not present yet – safe to ignore.
            pass
        try:
            conn.exec_driver_sql("ALTER TABLE downloads ADD COLUMN category VARCHAR(50)")
        except Exception:
            # Column already exists or table not present yet – safe to ignore.
            pass

