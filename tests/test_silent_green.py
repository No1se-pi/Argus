from types import SimpleNamespace

from app.alerts.service import AlertService


def _svc(silent_enabled: bool = True) -> AlertService:
    svc = AlertService.__new__(AlertService)
    svc.settings = SimpleNamespace(reviews_silent_green=silent_enabled)
    return svc


def _ai(status="SUCCESS", verdict="GREEN"):
    return SimpleNamespace(status=status, verdict=verdict)


def _rev(rating=5):
    return SimpleNamespace(rating=rating)


def test_green_success_review_is_silent():
    assert _svc()._review_is_silent(_rev(5), _ai()) is True


def test_four_stars_green_is_loud():
    assert _svc()._review_is_silent(_rev(4), _ai()) is False
    assert _svc()._review_is_silent(_rev(None), _ai()) is False


def test_yellow_red_are_loud():
    assert _svc()._review_is_silent(_rev(5), _ai(verdict="YELLOW")) is False
    assert _svc()._review_is_silent(_rev(1), _ai(verdict="RED")) is False


def test_uncertain_states_are_loud():
    assert _svc()._review_is_silent(_rev(5), None) is False
    assert _svc()._review_is_silent(_rev(5), _ai(status="PENDING")) is False
    assert _svc()._review_is_silent(_rev(5), _ai(status="FAILED")) is False


def test_low_rating_is_loud_even_if_green():
    assert _svc()._review_is_silent(_rev(3), _ai()) is False
    assert _svc()._review_is_silent(_rev(1), _ai()) is False


def test_feature_can_be_disabled():
    assert _svc(False)._review_is_silent(_rev(5), _ai()) is False
