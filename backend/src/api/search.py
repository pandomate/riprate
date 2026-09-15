import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.api.deps import require_auth
from src.database import SearchHistory, get_db
from src.providers import meta

router = APIRouter(tags=["search"])
_log = logging.getLogger("search")

_RECENT_LIMIT = 6   # kept per media_type, per user

_WAF_KEYWORDS = ("waf", "blocked", "cookie", "imdb_cookie")


class SearchResultOut(BaseModel):
    imdb_id: str
    title: str
    media_type: str
    year: int | None
    poster_url: str | None
    cast: str
    is_anime: bool = False
    original_language: str | None = None


@router.get("/search")
async def search(q: str = Query(..., min_length=1), _user=Depends(require_auth)):
    results = await meta.search(q)
    return [
        SearchResultOut(
            imdb_id=r.imdb_id,
            title=r.title,
            media_type=r.media_type,
            year=r.year,
            poster_url=r.poster_url,
            cast=r.cast,
            is_anime=r.is_anime,
            original_language=r.original_language,
        )
        for r in results
    ]


# ── Recent search history (per user, DB-backed) ────────────────────────────

class RecentIn(BaseModel):
    imdb_id: str
    title: str
    media_type: str            # movie | tvSeries
    year: int | None = None
    poster_url: str | None = None
    cast: str = ""
    is_anime: bool = False


def _pack(r: SearchHistory) -> dict:
    return {
        "imdb_id": r.imdb_id, "title": r.title, "media_type": r.media_type,
        "year": r.year, "poster_url": r.poster_url, "cast": r.cast or "",
        "is_anime": bool(getattr(r, "is_anime", False)),
    }


def _recent_payload(db: Session, user_id: int) -> dict:
    rows = (
        db.query(SearchHistory)
        .filter_by(user_id=user_id)
        .order_by(SearchHistory.clicked_at.desc())
        .all()
    )
    movies = [_pack(r) for r in rows if r.media_type == "movie"][:_RECENT_LIMIT]
    shows = [_pack(r) for r in rows if r.media_type == "tvSeries"][:_RECENT_LIMIT]
    return {"movies": movies, "shows": shows}


def _prune(db: Session, user_id: int, media_type: str) -> None:
    """Keep only the newest _RECENT_LIMIT rows for this user + media_type."""
    rows = (
        db.query(SearchHistory.id)
        .filter_by(user_id=user_id, media_type=media_type)
        .order_by(SearchHistory.clicked_at.desc())
        .all()
    )
    stale = [rid for (rid,) in rows[_RECENT_LIMIT:]]
    if stale:
        db.query(SearchHistory).filter(SearchHistory.id.in_(stale)).delete(synchronize_session=False)
        db.commit()


@router.get("/search/recent")
def recent_list(user=Depends(require_auth), db: Session = Depends(get_db)):
    return _recent_payload(db, user.id)


@router.post("/search/recent")
def recent_add(body: RecentIn, user=Depends(require_auth), db: Session = Depends(get_db)):
    if body.media_type not in ("movie", "tvSeries"):
        raise HTTPException(status_code=422, detail="media_type must be movie or tvSeries")
    row = db.query(SearchHistory).filter_by(user_id=user.id, imdb_id=body.imdb_id).first()
    if row:
        row.title = body.title
        row.media_type = body.media_type
        row.year = body.year
        row.poster_url = body.poster_url
        row.cast = body.cast or ""
        row.is_anime = body.is_anime
        row.clicked_at = datetime.utcnow()
    else:
        db.add(SearchHistory(
            user_id=user.id, imdb_id=body.imdb_id, media_type=body.media_type,
            title=body.title, year=body.year, poster_url=body.poster_url,
            cast=body.cast or "", is_anime=body.is_anime,
        ))
    db.commit()
    _prune(db, user.id, body.media_type)
    return _recent_payload(db, user.id)


@router.delete("/search/recent/{imdb_id}")
def recent_remove(imdb_id: str, user=Depends(require_auth), db: Session = Depends(get_db)):
    db.query(SearchHistory).filter_by(user_id=user.id, imdb_id=imdb_id).delete()
    db.commit()
    return _recent_payload(db, user.id)


@router.delete("/search/recent")
def recent_clear(user=Depends(require_auth), db: Session = Depends(get_db)):
    db.query(SearchHistory).filter_by(user_id=user.id).delete()
    db.commit()
    return _recent_payload(db, user.id)


@router.get("/seasons/{imdb_id}")
async def get_seasons(imdb_id: str, _user=Depends(require_auth)):
    try:
        seasons = await meta.get_seasons(imdb_id)
        return {"seasons": seasons}
    except RuntimeError as exc:
        msg = str(exc)
        _log.warning("get_seasons %s: %s", imdb_id, msg)
        if any(k in msg.lower() for k in _WAF_KEYWORDS):
            raise HTTPException(status_code=503, detail=msg)
        raise HTTPException(status_code=502, detail="Could not fetch seasons.")
    except Exception as exc:
        _log.error("get_seasons %s unexpected: %s", imdb_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not fetch seasons.")


@router.get("/episodes/{imdb_id}/{season}")
async def get_episodes(imdb_id: str, season: int, _user=Depends(require_auth)):
    try:
        info = await meta.get_episodes(imdb_id, season)
        return {
            "season": info.season_number,
            "episodes": [
                {"episode": e.episode, "title": e.title}
                for e in info.episodes
            ],
        }
    except RuntimeError as exc:
        msg = str(exc)
        _log.warning("get_episodes %s s%s: %s", imdb_id, season, msg)
        if any(k in msg.lower() for k in _WAF_KEYWORDS):
            raise HTTPException(status_code=503, detail=msg)
        raise HTTPException(status_code=502, detail=f"Could not fetch episodes for season {season}.")
    except Exception as exc:
        _log.error("get_episodes %s s%s unexpected: %s", imdb_id, season, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Could not fetch episodes for season {season}.")
