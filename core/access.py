from core.models import PVPlant


def plants_accessible_to(user):
    """Return plants visible to a user, including every plant for superusers."""
    queryset = PVPlant.objects.all()
    if not getattr(user, "is_superuser", False):
        queryset = queryset.filter(owner=user)
    return queryset
