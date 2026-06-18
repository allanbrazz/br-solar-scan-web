from __future__ import annotations

from core.views._imports import *


@login_required
def user_manual_view(request: HttpRequest) -> HttpResponse:
    return render(request, "manual/user_manual.html")
