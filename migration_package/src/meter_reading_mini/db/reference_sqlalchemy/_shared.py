from __future__ import annotations

from collections.abc import Callable
from datetime import date, tzinfo

from sqlalchemy.orm import Session

from api.models import ReadingSession
from application.meter_reading_sync_repositories import ReleaseDateCalculator
from application.timezone_settings import DEFAULT_APP_TIMEZONE


class SQLAlchemyRepositoryContext:
	"""Shared Session and runtime configuration for split SQLAlchemy repositories."""

	def __init__(
		self,
		session: Session,
		*,
		release_date_calculator: ReleaseDateCalculator | None = None,
		scheduled_timezone_provider: Callable[[], tzinfo] | None = None,
	) -> None:
		self._session = session
		self._release_date_calculator = release_date_calculator
		self._scheduled_timezone_provider = scheduled_timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)

	def set_release_date_calculator(self, release_date_calculator: ReleaseDateCalculator | None) -> None:
		self._release_date_calculator = release_date_calculator

	def set_scheduled_timezone_provider(self, scheduled_timezone_provider: Callable[[], tzinfo] | None) -> None:
		self._scheduled_timezone_provider = scheduled_timezone_provider or (lambda: DEFAULT_APP_TIMEZONE)

	@property
	def session(self) -> Session:
		return self._session

	def _resolve_release_date(self, *, business_date: date, reading_session: ReadingSession) -> date:
		if self._release_date_calculator is None:
			return business_date
		return self._release_date_calculator.compute_release_date(business_date, reading_session)