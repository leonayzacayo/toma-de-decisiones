from django.db.models.signals import post_save
from django.contrib.auth.models import User
from django.dispatch import receiver
from .models import Postulante

@receiver(post_save, sender=User)
def crear_postulante(sender, instance, created, **kwargs):
    if created:
        # Evitar crear un objeto Postulante para administradores y evaluadores staff
        if instance.is_staff or instance.is_superuser:
            return
        Postulante.objects.get_or_create(
            user=instance,
            defaults={
                'cedula': instance.username,
                'telefono': '',
                'ficha_completada': False,
                'nombre_completo': instance.get_full_name() or instance.username,
            }
        )
