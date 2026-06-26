from django.views.generic import ListView, DetailView, TemplateView, UpdateView
from django.views import View
from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse_lazy
from django.utils import timezone
from django.http import JsonResponse, HttpResponse
from django.db import models
from django.db.models import Q

import openpyxl

from apps.postulantes.models import Postulante, SolicitudBeca
from apps.postulantes.forms import EditarPostulanteStaffForm
from apps.parametros.forms import ObservacionEvaluacionForm
from apps.parametros.models import ParametroBeca, ReglaDesempate
from apps.usuarios.decoradores import EvaluadorRequeridoMixin, AdministradorRequeridoMixin
from apps.usuarios.models import LogAccion
from .models import Evaluacion
from .services import EvaluacionService


# ──────────────────────────────────────────────
# Panel del Evaluador
# ──────────────────────────────────────────────

class PanelEvaluadorView(EvaluadorRequeridoMixin, ListView):
    template_name = 'evaluaciones/panel_evaluador.html'
    context_object_name = 'solicitudes'

    def get_queryset(self):
        # 1. Construir orden dinámico para desempate
        order_fields = ['-puntaje_total']
        rules = ReglaDesempate.objects.filter(activo=True).order_by('orden_ejecucion')
        for rule in rules:
            prefix = '-' if rule.direccion == 'desc' else ''
            order_fields.append(prefix + rule.campo_modelo)

        # 2. Obtener solicitudes con ficha completa ordenadas, excluyendo rechazados
        qs = SolicitudBeca.objects.select_related(
            'postulante__user',
            'postulante__ficha_socioeconomica',
            'postulante__datos_academicos'
        ).filter(postulante__ficha_completada=True, rechazado=False).order_by(*order_fields)

        # Convertir a lista para asignar la posición 1-N
        solicitudes = list(qs)
        for idx, sol in enumerate(solicitudes, 1):
            sol.posicion = idx
        return solicitudes

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        param_cupos = ParametroBeca.objects.filter(nombre='cupos_disponibles').first()
        ctx['cupos_disponibles'] = int(param_cupos.valor) if param_cupos else 10
        ctx['total_completados'] = len(self.object_list)
        ctx['total_seleccionados'] = sum(1 for sol in self.object_list if sol.estado == 'Beca Asignada')
        
        # Filtro de postulantes válidos (excluyendo staff/administradores/evaluadores)
        postulantes_validos = Postulante.objects.exclude(
            Q(user__is_staff=True) |
            Q(user__is_superuser=True) |
            Q(user__perfil__rol='evaluador') |
            Q(user__perfil__rol='administrador')
        )
        
        # Totales requeridos por el usuario
        ctx['total_cuentas'] = postulantes_validos.count()
        ctx['total_pendientes_aprobar'] = postulantes_validos.filter(
            ficha_completada=True
        ).filter(
            Q(solicitud_beca__isnull=True) | Q(solicitud_beca__estado__in=['Pendiente', 'Postulación completada'])
        ).count()
        ctx['total_incompletos'] = postulantes_validos.filter(ficha_completada=False).count()

        # Datos para gráfico de distribución por carrera
        # Solo mostrar las carreras válidas disponibles en la ficha socioeconómica
        CARRERAS_VALIDAS = [
            'Ingeniería en Sistemas',
            'Ingeniería Agropecuaria',
            'Contaduría Pública',
            'Industrialización de Alimentos',
        ]
        # Mapeo de variantes de nombres hacia el nombre canónico
        VARIANTES_CARRERA = {
            'Ing. en Sistemas': 'Ingeniería en Sistemas',
            'Ing. en Agropecuaria': 'Ingeniería Agropecuaria',
            'Lic. en Contaduría': 'Contaduría Pública',
        }

        # Contar postulantes por carrera (incluyendo variantes de nombre)
        conteo_carreras = {c: 0 for c in CARRERAS_VALIDAS}
        for p in postulantes_validos.values_list('carrera', flat=True):
            carrera_norm = VARIANTES_CARRERA.get(p, p)
            if carrera_norm in conteo_carreras:
                conteo_carreras[carrera_norm] += 1

        ctx['carrera_data'] = {
            'labels': CARRERAS_VALIDAS,
            'values': [conteo_carreras[c] for c in CARRERAS_VALIDAS],
        }

        # Datos para gráfico de estados
        estados_qs = SolicitudBeca.objects.filter(
            postulante__in=postulantes_validos
        ).values('estado').annotate(total=models.Count('id'))
        
        estados_dict = {
            'Pendiente': 0,
            'Postulación completada': 0,
            'Beca Asignada': 0,
            'No seleccionado': 0,
            'Rechazado': 0,
        }
        for item in estados_qs:
            estado = item['estado']
            if estado in estados_dict:
                estados_dict[estado] = item['total']

        ctx['estado_data'] = {
            'labels': ['Incompletas', 'Pendientes de Aprobación', 'Becas Asignadas', 'No Seleccionados', 'Rechazados'],
            'values': [
                ctx['total_incompletos'],
                ctx['total_pendientes_aprobar'],
                estados_dict['Beca Asignada'],
                estados_dict['No seleccionado'],
                estados_dict['Rechazado'],
            ]
        }
        
        return ctx


class EjecutarOptimizacionView(EvaluadorRequeridoMixin, View):
    def post(self, request):
        try:
            # 1. Obtener cupos disponibles
            param_cupos = ParametroBeca.objects.filter(nombre='cupos_disponibles').first()
            n_cupos = int(param_cupos.valor) if param_cupos else 10

            # 2. Construir orden dinámico para desempate
            order_fields = ['-puntaje_total']
            
            rules = ReglaDesempate.objects.filter(activo=True).order_by('orden_ejecucion')
            for rule in rules:
                prefix = '-' if rule.direccion == 'desc' else ''
                order_fields.append(prefix + rule.campo_modelo)

            # 3. Obtener solicitudes con estado 'Aprobado', excluyendo rechazados
            solicitudes = SolicitudBeca.objects.select_related('postulante', 'postulante__user').filter(
                postulante__ficha_completada=True,
                estado='Aprobado',
                rechazado=False
            ).order_by(*order_fields)

            total_solicitudes = solicitudes.count()

            # 4. Asignar becas
            now = timezone.now()
            seleccionados_ids = []
            from django.core.mail import get_connection, EmailMessage
            from django.conf import settings
            
            # Reutilizar una única conexión SMTP para agilizar el envío masivo
            emails_to_send = []
            connection = get_connection()
            
            # Tomar los primeros N
            for idx, sol in enumerate(solicitudes):
                if idx < n_cupos:
                    sol.estado = 'Beca Asignada'
                    sol.fecha_asignacion = now
                    seleccionados_ids.append(sol.pk)
                    
                    asunto = "¡Felicidades! Beca Albergue Asignada"
                    p_total = sol.puntaje_total if sol.puntaje_total is not None else 0.0
                    cuerpo = (
                        f"Hola {sol.postulante.user.first_name or sol.postulante.nombre_completo or sol.postulante.user.username},\n\n"
                        f"Nos complace informarte que has sido seleccionado para la asignación de la Beca Albergue UAGRM.\n\n"
                        f"Tu puntaje final obtenido es de {p_total:.2f} puntos.\n\n"
                        f"Puedes verificar los detalles en tu panel de usuario:\n"
                        f"{request.build_absolute_uri('/dashboard/')}"
                    )
                else:
                    sol.estado = 'No seleccionado'
                    sol.fecha_asignacion = None
                    
                    asunto = "Resultado del proceso de selección - Beca Albergue"
                    cuerpo = (
                        f"Hola {sol.postulante.user.first_name or sol.postulante.nombre_completo or sol.postulante.user.username},\n\n"
                        f"Te informamos que ha finalizado el proceso de asignación de la Beca Albergue UAGRM.\n\n"
                        f"Lamentablemente, en esta ocasión no has sido seleccionado dentro de los cupos disponibles para esta convocatoria.\n\n"
                        f"Puedes verificar los detalles en tu panel de usuario:\n"
                        f"{request.build_absolute_uri('/dashboard/')}"
                    )
                
                # Preparar correo si el usuario tiene email
                if sol.postulante.user.email:
                    email = EmailMessage(
                        asunto,
                        cuerpo,
                        getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@uagrm.edu.bo'),
                        [sol.postulante.user.email],
                    )
                    emails_to_send.append(email)
                    
                sol.save()
                
            # Enviar todos los correos en bloque
            try:
                if emails_to_send:
                    connection.send_messages(emails_to_send)
            except Exception as e:
                print(f"Error al enviar correos masivos: {e}")

            LogAccion.objects.create(
                usuario=request.user,
                accion='evaluacion',
                detalles={
                    'mensaje': 'Optimización de becas ejecutada',
                    'cupos': n_cupos,
                    'total_postulantes': total_solicitudes,
                    'seleccionados_count': len(seleccionados_ids),
                }
            )

            messages.success(
                request,
                f'Optimización ejecutada con éxito. Se asignaron {len(seleccionados_ids)} becas de {total_solicitudes} postulantes activos.'
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.error(request, f"Error al ejecutar la optimización: {str(e)}")
            
        return redirect('evaluaciones:panel_evaluador')


class ExportarExcelView(EvaluadorRequeridoMixin, View):
    def get(self, request):
        # Crear libro Excel
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Postulantes Seleccionados"

        # Cabeceras
        headers = [
            "Nombres", "Apellidos", "Cédula", "Correo", 
            "Facultad", "Carrera", "Puntaje Académico", 
            "Puntaje Socioeconómico", "Puntaje Total (Z)", "Estado"
        ]
        ws.append(headers)

        # Obtener todas las solicitudes
        solicitudes = SolicitudBeca.objects.select_related(
            'postulante__user'
        ).filter(postulante__ficha_completada=True).order_by('-puntaje_total')

        for sol in solicitudes:
            post = sol.postulante
            user = post.user
            
            # Obtener datos de facultad y carrera
            facultad = post.facultad
            carrera = post.carrera

            ws.append([
                user.first_name,
                user.last_name,
                post.cedula,
                user.email,
                facultad,
                carrera,
                sol.puntaje_academico,
                sol.puntaje_socioeconomico,
                sol.puntaje_total,
                sol.estado
            ])

        # Formatear HttpResponse
        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename="reporte_seleccionados_beca.xlsx"'
        wb.save(response)
        return response


class RankingPostulantesView(EvaluadorRequeridoMixin, ListView):
    template_name = 'evaluaciones/ranking.html'
    context_object_name = 'solicitudes'

    def get_queryset(self):
        # 1. Construir orden dinámico para desempate
        order_fields = ['-puntaje_total']
        
        rules = ReglaDesempate.objects.filter(activo=True).order_by('orden_ejecucion')
        for rule in rules:
            prefix = '-' if rule.direccion == 'desc' else ''
            order_fields.append(prefix + rule.campo_modelo)

        # 2. Obtener solicitudes con ficha completa ordenadas
        qs = SolicitudBeca.objects.select_related(
            'postulante__user',
            'postulante__ficha_socioeconomica',
            'postulante__datos_academicos'
        ).filter(postulante__ficha_completada=True, rechazado=False).order_by(*order_fields)

        # Convertir a lista para asignar la posición 1-N
        solicitudes = list(qs)
        for idx, sol in enumerate(solicitudes, 1):
            sol.posicion = idx
        return solicitudes

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        param_cupos = ParametroBeca.objects.filter(nombre='cupos_disponibles').first()
        ctx['cupos_disponibles'] = int(param_cupos.valor) if param_cupos else 10
        ctx['total_completados'] = len(self.object_list)
        ctx['total_seleccionados'] = sum(1 for sol in self.object_list if sol.estado == 'Beca Asignada')
        return ctx


class ExportarRankingExcelView(EvaluadorRequeridoMixin, View):
    def get(self, request):
        order_fields = ['-puntaje_total']
        rules = ReglaDesempate.objects.filter(activo=True).order_by('orden_ejecucion')
        for rule in rules:
            prefix = '-' if rule.direccion == 'desc' else ''
            order_fields.append(prefix + rule.campo_modelo)

        solicitudes = SolicitudBeca.objects.select_related(
            'postulante__user',
            'postulante__ficha_socioeconomica',
            'postulante__datos_academicos'
        ).filter(postulante__ficha_completada=True, rechazado=False).order_by(*order_fields)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Ranking de Postulantes"

        headers = [
            "Posición", "Nombres", "Apellidos", "Cédula", "Correo", 
            "Facultad", "Carrera", "Puntaje Académico", 
            "Puntaje Socioeconómico", "Puntaje Total (Z)", "Estado"
        ]
        ws.append(headers)

        for idx, sol in enumerate(solicitudes, 1):
            post = sol.postulante
            user = post.user
            
            facultad = post.facultad
            carrera = post.carrera

            ws.append([
                idx,
                user.first_name,
                user.last_name,
                post.cedula,
                user.email,
                facultad,
                carrera,
                sol.puntaje_academico or 0.0,
                sol.puntaje_socioeconomico or 0.0,
                sol.puntaje_total or 0.0,
                sol.estado
            ])

        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = 'attachment; filename="ranking_postulantes.xlsx"'
        wb.save(response)
        return response


# ──────────────────────────────────────────────
# Vistas Anteriores (Mantener para compatibilidad)
# ──────────────────────────────────────────────

class ListaPostulantesView(EvaluadorRequeridoMixin, ListView):
    template_name = 'evaluaciones/lista.html'
    context_object_name = 'postulantes'
    paginate_by = 20

    def get_queryset(self):
        qs = Postulante.objects.select_related(
            'ficha_socioeconomica', 'evaluacion', 'convocatoria', 'solicitud_beca', 'user__perfil'
        ).exclude(
            Q(user__is_staff=True) |
            Q(user__is_superuser=True) |
            Q(user__perfil__rol='evaluador') |
            Q(user__perfil__rol='administrador')
        ).order_by('-fecha_registro')

        estado = self.request.GET.get('estado')
        if estado:
            if estado == 'Pendiente':
                qs = qs.filter(Q(solicitud_beca__isnull=True) | Q(solicitud_beca__estado='Pendiente'))
            else:
                qs = qs.filter(solicitud_beca__estado=estado)

        busqueda = self.request.GET.get('q')
        if busqueda:
            qs = qs.filter(
                cedula__icontains=busqueda
            ) | qs.filter(nombre_completo__icontains=busqueda)

        return qs.distinct()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['estados'] = [
            ('Pendiente', 'Pendiente'),
            ('Aprobado', 'Aprobado'),
            ('Rechazado', 'Rechazado'),
            ('Beca Asignada', 'Beca Asignada'),
            ('No seleccionado', 'No seleccionado'),
        ]
        ctx['filtro_estado'] = self.request.GET.get('estado', '')
        ctx['filtro_q'] = self.request.GET.get('q', '')
        ctx['total'] = self.get_queryset().count()
        return ctx


class DetallePostulanteView(EvaluadorRequeridoMixin, DetailView):
    model = Postulante
    template_name = 'evaluaciones/detalle.html'
    context_object_name = 'postulante'

    def get_queryset(self):
        return Postulante.objects.select_related(
            'ficha_socioeconomica',
            'evaluacion',
            'convocatoria',
            'user',
        )

    def get_context_data(self, **kwargs):
        # Recalcular puntaje antes de mostrar para asegurar que esté al día
        from apps.postulantes.views import calcular_y_guardar_puntaje
        try:
            calcular_y_guardar_puntaje(self.object)
            self.object.refresh_from_db()
        except Exception as e:
            print(f"Error al recalcular puntaje en detalle: {e}")

        ctx = super().get_context_data(**kwargs)
        ctx['obs_form'] = ObservacionEvaluacionForm()

        # Deserializar dirección e infraestructura extra de JSON
        import json
        direccion_extra = {}
        if self.object.direccion:
            try:
                direccion_extra = json.loads(self.object.direccion)
            except Exception:
                pass
        ctx['direccion_extra'] = direccion_extra

        # getattr() no captura RelatedObjectDoesNotExist en relaciones OneToOne de Django.
        # Se usa try/except para evitar error 500 cuando el postulante aún no tiene evaluación
        # ni solicitud de beca creada (por ejemplo si nunca envió su postulación).
        try:
            ctx['evaluacion'] = self.object.evaluacion
        except Exception:
            ctx['evaluacion'] = None

        try:
            ctx['solicitud_beca'] = self.object.solicitud_beca
        except Exception:
            ctx['solicitud_beca'] = None

        try:
            ctx['tiene_datos_completos'] = bool(self.object.ficha_socioeconomica)
        except Exception:
            ctx['tiene_datos_completos'] = False

        return ctx

    def post(self, request, *args, **kwargs):
        try:
            from django.core.mail import send_mail
            from django.conf import settings
            
            postulante = self.get_object()
            solicitud, _ = SolicitudBeca.objects.get_or_create(postulante=postulante)

            if 'aprobar' in request.POST:
                solicitud.estado = 'Aprobado'
                solicitud.fecha_revision = timezone.now()
                solicitud.motivo_rechazo = None
                solicitud.save()

                postulante.ficha_completada = True
                postulante.save()

                # Registrar en LogAccion
                LogAccion.objects.create(
                    usuario=request.user,
                    accion='evaluacion',
                    detalles={'postulante': postulante.user.username, 'nuevo_estado': 'Aprobado'},
                    objeto_id=postulante.pk,
                    objeto_tipo='Postulante'
                )

                # Enviar correo de aprobación al postulante
                try:
                    asunto = "Tu postulación a la Beca Albergue UAGRM ha sido Aprobada"
                    cuerpo = (
                        f"Hola {postulante.user.first_name or postulante.nombre_completo or postulante.user.username},\n\n"
                        f"Nos complace informarte que tu postulación al programa de Beca Albergue ha sido aprobada por el comité evaluador.\n\n"
                        f"Tu postulación ahora pasará al proceso de selección final y optimización de cupos disponibles.\n\n"
                        f"Puedes hacer seguimiento en tu panel de usuario en:\n"
                        f"{request.build_absolute_uri('/dashboard/')}"
                    )
                    send_mail(
                        asunto,
                        cuerpo,
                        getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@uagrm.edu.bo'),
                        [postulante.user.email],
                        fail_silently=False
                    )
                except Exception as e:
                    print(f"Error al enviar correo de aprobación: {e}")

                messages.success(request, f'Postulación de {postulante.nombre_completo} aprobada correctamente.')
                return redirect('evaluaciones:detalle', pk=postulante.pk)

            elif 'rechazar' in request.POST:
                motivo = request.POST.get('motivo_rechazo', '').strip()
                if not motivo or len(motivo) < 10:
                    messages.error(request, 'El motivo del rechazo es obligatorio y debe tener al menos 10 caracteres.')
                    return redirect('evaluaciones:detalle', pk=postulante.pk)

                solicitud.estado = 'Rechazado'
                solicitud.motivo_rechazo = motivo
                solicitud.fecha_revision = timezone.now()
                solicitud.save()

                postulante.ficha_completada = True
                postulante.save()

                # Registrar en LogAccion
                LogAccion.objects.create(
                    usuario=request.user,
                    accion='evaluacion',
                    detalles={'postulante': postulante.user.username, 'nuevo_estado': 'Rechazado', 'motivo': motivo},
                    objeto_id=postulante.pk,
                    objeto_tipo='Postulante'
                )

                # Enviar correo
                try:
                    asunto = "Tu postulación a la Beca Albergue ha sido rechazada"
                    cuerpo = f"Hola {postulante.user.first_name or postulante.nombre_completo},\n\nTu postulación ha sido rechazada por el siguiente motivo:\n\n{motivo}\n\nPuedes ver más detalles en tu panel de usuario en:\n{request.build_absolute_uri('/dashboard/mi-postulacion/')}"
                    send_mail(
                        asunto,
                        cuerpo,
                        getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@uagrm.edu.bo'),
                        [postulante.user.email],
                        fail_silently=False
                    )
                except Exception as e:
                    print(f"Error al enviar correo: {e}")

                messages.success(request, f'Postulación de {postulante.nombre_completo} rechazada (notificación de correo omitida o con error).')
                return redirect('evaluaciones:detalle', pk=postulante.pk)

            return redirect('evaluaciones:detalle', pk=postulante.pk)

        except Exception as e:
            import traceback
            traceback.print_exc()
            messages.error(request, f"Error al procesar la postulación: {str(e)}")
            pk = self.kwargs.get('pk') or (self.object.pk if hasattr(self, 'object') and self.object else None)
            if pk:
                return redirect('evaluaciones:detalle', pk=pk)
            return redirect('evaluaciones:panel_evaluador')


class EvaluarPostulanteView(EvaluadorRequeridoMixin, TemplateView):
    def post(self, request, pk):
        postulante = get_object_or_404(
            Postulante.objects.select_related('ficha_socioeconomica'),
            pk=pk,
        )
        try:
            evaluacion = EvaluacionService.evaluar(postulante, evaluado_por=request.user)
            messages.success(
                request,
                f'Evaluación completada: {postulante.nombre_completo} → ({evaluacion.puntaje_total} pts)',
            )
        except Exception as exc:
            messages.error(request, f'Error al evaluar: {exc}')

        return redirect('evaluaciones:detalle', pk=pk)


class ReevaluarMasivoView(EvaluadorRequeridoMixin, TemplateView):
    template_name = 'evaluaciones/reevaluar_masivo.html'

    def post(self, request):
        return redirect('evaluaciones:lista')


class EditarPostulanteStaffView(EvaluadorRequeridoMixin, UpdateView):
    model = Postulante
    form_class = EditarPostulanteStaffForm
    template_name = 'evaluaciones/editar_postulante.html'

    def get_success_url(self):
        return reverse_lazy('evaluaciones:detalle', kwargs={'pk': self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, 'Datos del postulante actualizados correctamente.')
        return response


from django.contrib.auth.decorators import login_required, user_passes_test

@login_required
@user_passes_test(lambda u: u.is_staff or (hasattr(u, 'perfil') and u.perfil.es_evaluador()))
def rechazar_postulante(request, pk):
    # Usar get_or_create en lugar de get_object_or_404 para que funcione aunque
    # el postulante aún no tenga SolicitudBeca (no haya enviado su postulación todavía).
    postulante_obj = get_object_or_404(Postulante, pk=pk)
    solicitud, _ = SolicitudBeca.objects.get_or_create(postulante=postulante_obj)

    if request.method == 'POST':
        motivo = request.POST.get('motivo_rechazo', '').strip()
        if not motivo or len(motivo) < 10:
            messages.error(request, 'El motivo del rechazo es obligatorio y debe tener al menos 10 caracteres.')
            return redirect('evaluaciones:detalle', pk=pk)

        notificar = request.POST.get('notificar') == 'on' or request.POST.get('notificar') == 'true'
        solicitud.rechazado = True
        solicitud.motivo_rechazo = motivo
        solicitud.fecha_rechazo = timezone.now()
        solicitud.estado = 'Rechazado'
        solicitud.notificado_rechazo = notificar
        solicitud.save()

        postulante_obj.ficha_completada = True
        postulante_obj.save()

        if notificar:
            try:
                from django.core.mail import send_mail
                from django.conf import settings
                asunto = "Actualización de tu postulación a la Beca Albergue UAGRM"
                cuerpo = f"Tu postulación ha sido rechazada por el siguiente motivo: {motivo}.\n\nPuedes contactar al comité de becas para más información."
                send_mail(
                    asunto,
                    cuerpo,
                    getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@uagrm.edu.bo'),
                    [solicitud.postulante.user.email],
                    fail_silently=True
                )
            except Exception as e:
                print(f"Error al enviar correo en rechazo individual: {e}")

        # Registrar en LogAccion
        LogAccion.objects.create(
            usuario=request.user,
            accion='evaluacion',
            detalles={'postulante': solicitud.postulante.user.username, 'nuevo_estado': 'Rechazado', 'motivo': motivo},
            objeto_id=solicitud.postulante.pk,
            objeto_tipo='Postulante'
        )

        messages.success(request, f'Postulante {solicitud.postulante.user.get_full_name()} rechazado correctamente.')
        # Usar namespace completo para evitar NoReverseMatch en producción
        return redirect('evaluaciones:panel_evaluador')

    return redirect('evaluaciones:detalle', pk=pk)


@login_required
@user_passes_test(lambda u: u.is_staff or (hasattr(u, 'perfil') and u.perfil.es_evaluador()))
def reactivar_postulante(request, pk):
    # Usar get_or_create para que no falle si el postulante no tiene SolicitudBeca todavía.
    postulante_obj = get_object_or_404(Postulante, pk=pk)
    solicitud, _ = SolicitudBeca.objects.get_or_create(postulante=postulante_obj)

    solicitud.rechazado = False
    solicitud.estado = 'Pendiente'
    solicitud.motivo_rechazo = None
    solicitud.fecha_rechazo = None
    solicitud.notificado_rechazo = False
    solicitud.save()

    # Registrar en LogAccion
    LogAccion.objects.create(
        usuario=request.user,
        accion='evaluacion',
        detalles={'postulante': solicitud.postulante.user.username, 'nuevo_estado': 'Pendiente', 'mensaje': 'Postulante reactivado'},
        objeto_id=solicitud.postulante.pk,
        objeto_tipo='Postulante'
    )

    messages.success(request, f'Postulante {solicitud.postulante.user.get_full_name()} reactivado correctamente.')
    return redirect('evaluaciones:detalle', pk=pk)


@login_required
@user_passes_test(lambda u: u.is_staff or (hasattr(u, 'perfil') and u.perfil.es_evaluador()))
def rechazar_y_reasignar_beca(request, pk):
    postulante_obj = get_object_or_404(Postulante, pk=pk)
    solicitud = get_object_or_404(SolicitudBeca, postulante=postulante_obj)

    if request.method == 'POST':
        motivo = request.POST.get('motivo_rechazo', 'Perdió el beneficio (ya contaba con otra beca u otra incompatibilidad).').strip()
        
        had_beca = (solicitud.estado == 'Beca Asignada')
        
        # 1. Rechazar al postulante actual
        solicitud.rechazado = True
        solicitud.estado = 'Rechazado'
        solicitud.motivo_rechazo = motivo
        solicitud.fecha_rechazo = timezone.now()
        solicitud.save()

        postulante_obj.ficha_completada = True
        postulante_obj.save()

        # Sincronizar Evaluacion
        eval_obj, _ = Evaluacion.objects.get_or_create(postulante=postulante_obj)
        eval_obj.estado = Evaluacion.ESTADO_RECHAZADO
        eval_obj.save()

        # Registrar en LogAccion
        LogAccion.objects.create(
            usuario=request.user,
            accion='evaluacion',
            detalles={'postulante': solicitud.postulante.user.username, 'nuevo_estado': 'Rechazado', 'motivo': motivo},
            objeto_id=solicitud.postulante.pk,
            objeto_tipo='Postulante'
        )

        # 2. Si tenía beca asignada, reasignar el cupo al siguiente postulante elegible
        sig_postulante = None
        if had_beca:
            # Obtener el orden de desempate
            order_fields = ['-puntaje_total']
            rules = ReglaDesempate.objects.filter(activo=True).order_by('orden_ejecucion')
            for rule in rules:
                prefix = '-' if rule.direccion == 'desc' else ''
                order_fields.append(prefix + rule.campo_modelo)

            # Buscar el primer postulante con ficha completa, no rechazado, que no tenga beca asignada
            siguiente_solicitud = SolicitudBeca.objects.filter(
                postulante__ficha_completada=True,
                rechazado=False,
                estado='No seleccionado'
            ).order_by(*order_fields).first()

            if siguiente_solicitud:
                siguiente_solicitud.estado = 'Beca Asignada'
                siguiente_solicitud.fecha_asignacion = timezone.now()
                siguiente_solicitud.save()
                sig_postulante = siguiente_solicitud.postulante
                
                # Sincronizar Evaluacion para el nuevo beneficiario
                eval_sig, _ = Evaluacion.objects.get_or_create(postulante=sig_postulante)
                eval_sig.estado = Evaluacion.ESTADO_APROBADO
                eval_sig.save()
                
                # Enviar correo de asignación al siguiente
                try:
                    from django.core.mail import send_mail
                    from django.conf import settings
                    asunto = "¡Felicidades! Beca Albergue Asignada"
                    cuerpo = (
                        f"Hola {sig_postulante.user.first_name or sig_postulante.nombre_completo or sig_postulante.user.username},\n\n"
                        f"Te informamos que has sido seleccionado para la asignación de la Beca Albergue UAGRM por reasignación de cupo libre.\n\n"
                        f"Puedes verificar los detalles en tu panel de usuario:\n"
                        f"{request.build_absolute_uri('/dashboard/')}"
                    )
                    send_mail(
                        asunto,
                        cuerpo,
                        getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@uagrm.edu.bo'),
                        [sig_postulante.user.email],
                        fail_silently=True
                    )
                except Exception as e:
                    print(f"Error al enviar correo de reasignación: {e}")

        msg = f'Postulante {solicitud.postulante.user.get_full_name()} rechazado.'
        if sig_postulante:
            msg += f' El cupo libre ha sido reasignado automáticamente a {sig_postulante.user.get_full_name()} por orden de mérito.'
        
        messages.success(request, msg)
        return redirect('evaluaciones:ranking_postulantes')
        
    return redirect('evaluaciones:ranking_postulantes')


class VerFichaSocioeconomicaView(EvaluadorRequeridoMixin, TemplateView):
    template_name = 'postulantes/ficha_socioeconomica.html'

    def get(self, request, pk, *args, **kwargs):
        from apps.postulantes.forms import FichaSocioeconomicaForm, DatosAcademicosForm, MiembroFamiliarFormSet
        postulante = get_object_or_404(Postulante, pk=pk)
        ficha = getattr(postulante, 'ficha_socioeconomica', None)
        academicos = getattr(postulante, 'datos_academicos', None)

        form_ficha = FichaSocioeconomicaForm(instance=ficha)
        form_acad = DatosAcademicosForm(instance=academicos)
        formset = MiembroFamiliarFormSet(instance=ficha)

        # Disable all fields for read-only view
        for field in form_ficha.fields.values():
            field.widget.attrs['disabled'] = True
            field.required = False
        for field in form_acad.fields.values():
            field.widget.attrs['disabled'] = True
            field.required = False
        for form in formset:
            for field in form.fields.values():
                field.widget.attrs['disabled'] = True
                field.required = False

        # Deserializar dirección e infraestructura extra de JSON
        import json
        direccion_extra = {}
        if postulante.direccion:
            try:
                direccion_extra = json.loads(postulante.direccion)
            except Exception:
                direccion_extra = {
                    'calle': postulante.direccion,
                    'cant_dormitorios': '1',
                    'cant_banos': '1',
                }

        return self.render_to_response({
            'form_ficha': form_ficha,
            'form_acad': form_acad,
            'formset': formset,
            'postulante': postulante,
            'puede_editar': False,    # El evaluador solo puede ver, no editar
            'es_evaluador': True,     # Indica que es vista del evaluador para mostrar botones correctos
            'direccion_extra': direccion_extra,
        })

