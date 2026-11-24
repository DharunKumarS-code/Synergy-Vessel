from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from ..services.image_service import ImageNotFound, InvalidImageId
from ..services.inspection_store import InspectionStore
from ..auth.deps import RequireMediaUser
from .deps import get_store

router = APIRouter(prefix="/api", tags=["images"])


@router.get("/images/{image_id}", response_class=FileResponse)
def get_image(image_id: str, _: RequireMediaUser, store: InspectionStore = Depends(get_store)) -> FileResponse:
    """SV-02: needs a live session -- the photo cookie (for <img> tags) or the bearer header. Never public."""
    try:
        path, media = store.images.resolve(image_id)
    except InvalidImageId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ImageNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return FileResponse(path, media_type=media, headers={"Cache-Control": "private, no-cache"})
