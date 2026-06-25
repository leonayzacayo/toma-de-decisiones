from django import forms
from django.forms import inlineformset_factory
from django.views.generic import TemplateView
from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404
from django.db import transaction
from django.db import models

from .models import Postulante, FichaSocioeconomica, DatosAcademicos, SolicitudBeca, MateriaSemestre
from .forms import FichaSocioeconomicaForm, DatosAcademicosForm, MiembroFamiliarFormSet, RegistroAcademicoForm
from apps.usuarios.decoradores import PostulanteRequeridoMixin, LoginRequeridoMixin
from apps.parametros.models import RangoPPS, RangoMaterias, OpcionSocioeconomica


def calcular_y_guardar_puntaje(postulante):
    # 1. Puntaje Académico (recalcula y guarda usando la nueva lógica)
    from .utils import calcular_puntaje_academico
    p_academico = 0.0
    try:
        res = calcular_puntaje_academico(postulante)
        p_academico = res['total']
    except Exception as e:
        print(f"Error calculando puntaje académico: {e}")
        try:
            p_academico = postulante.datos_academicos.puntaje_academico_total
        except Exception:
            p_academico = 0.0

    # 2. Puntaje Socioeconómico
    p_socioeconomico = 0.0
    try:
        ficha = postulante.ficha_socioeconomica
        
        # Mapeo de campos textuales directos (excluyendo procedencia para tratarlo dinámicamente)
        campos_texto = {
            'dependencia': ficha.dependencia,
            'ocupacion': ficha.ocupacion,
            'rango_ingresos': ficha.rango_ingresos,
            'lugar_residencia': ficha.lugar_residencia,
            'tenencia_vivienda': ficha.tenencia_vivienda,
            'tipo_vivienda': ficha.tipo_vivienda,
        }
        
        for var_name, value in campos_texto.items():
            opt = OpcionSocioeconomica.objects.filter(variable=var_name, opcion_texto=value).first()
            if opt:
                p_socioeconomico += opt.puntaje

        # Determinar puntaje de procedencia dinámicamente
        proc_val = ficha.procedencia or ''
        parts = [p.strip() for p in proc_val.split('-')]
        
        proc_categoria = "Ciudad"
        if len(parts) == 3:
            dep, prov, mun = parts
            if dep != "Santa Cruz":
                proc_categoria = "Otro departamento"
            elif prov != "Andrés Ibáñez":
                proc_categoria = "Provincia"
            else:
                proc_categoria = "Ciudad"
        elif proc_val in ["Ciudad", "Otro departamento", "Provincia"]:
            proc_categoria = proc_val
            
        opt_proc = OpcionSocioeconomica.objects.filter(variable='procedencia', opcion_texto=proc_categoria).first()
        if opt_proc:
            p_socioeconomico += opt_proc.puntaje

        # Mapeo de num_integrantes (X4)
        num = ficha.num_integrantes
        if num <= 1:
            x4_txt = "Hasta 1 miembro"
        elif num in (2, 3):
            x4_txt = "De 2 a 3 miembros"
        elif num == 4:
            x4_txt = "De 3 a 4 miembros"
        else:
            x4_txt = "Más de 4 miembros"
        opt_x4 = OpcionSocioeconomica.objects.filter(variable='num_integrantes', opcion_texto=x4_txt).first()
        if opt_x4:
            p_socioeconomico += opt_x4.puntaje

        # Mapeo de num_hijos (X5)
        hijos = ficha.num_hijos
        if hijos == 0:
            x5_txt = "Sin hijos"
        elif hijos == 1:
            x5_txt = "1 hijo"
        else:
            x5_txt = "Más de 1 hijo"
        opt_x5 = OpcionSocioeconomica.objects.filter(variable='num_hijos', opcion_texto=x5_txt).first()
        if opt_x5:
            p_socioeconomico += opt_x5.puntaje

        p_socioeconomico = min(p_socioeconomico, 70.0)
    except Exception as e:
        p_socioeconomico = 0.0

    p_total = p_academico + p_socioeconomico

    # Verificar procedencia para rechazo automático por Vallegrande
    proc_val = ""
    try:
        proc_val = postulante.ficha_socioeconomica.procedencia or ""
    except Exception:
        pass

    es_vallegrande = False
    parts = [p.strip() for p in proc_val.split('-')]
    if len(parts) == 3 and parts[2].lower() == "vallegrande":
        es_vallegrande = True
    elif proc_val.strip().lower() == "vallegrande":
        es_vallegrande = True

    # Guardar o actualizar SolicitudBeca
    solicitud, _ = SolicitudBeca.objects.get_or_create(postulante=postulante)
    solicitud.puntaje_academico = p_academico
    solicitud.puntaje_socioeconomico = p_socioeconomico
    solicitud.puntaje_total = p_total

    if es_vallegrande:
        if postulante.ficha_completada:
            solicitud.rechazado = True
            solicitud.estado = 'Rechazado'
            solicitud.motivo_rechazo = "No cumples con los requisitos de procedencia, ya que resides en el municipio de Vallegrande."
            from django.utils import timezone
            solicitud.fecha_rechazo = timezone.now()
        else:
            solicitud.rechazado = False
            if solicitud.estado in (None, '', 'Pendiente', 'Postulación completada', 'Rechazado'):
                solicitud.estado = "Pendiente"
                solicitud.motivo_rechazo = None
                solicitud.fecha_rechazo = None
    else:
        # Revertir rechazo automático si cambia su procedencia a algo que no sea Vallegrande
        if solicitud.rechazado and solicitud.motivo_rechazo == "No cumples con los requisitos de procedencia, ya que resides en el municipio de Vallegrande.":
            solicitud.rechazado = False
            solicitud.estado = 'Pendiente'
            solicitud.motivo_rechazo = None
            solicitud.fecha_rechazo = None

        if solicitud.estado in (None, '', 'Pendiente', 'Postulación completada'):
            solicitud.estado = "Postulación completada" if postulante.ficha_completada else "Pendiente"
            
    solicitud.save()

    # Sincronizar con el modelo legado Evaluacion
    from apps.evaluaciones.models import Evaluacion
    evaluacion, _ = Evaluacion.objects.get_or_create(postulante=postulante)
    evaluacion.puntaje_academico = p_academico
    evaluacion.puntaje_socioeconomico = p_socioeconomico
    evaluacion.puntaje_total = p_total
    
    if solicitud.estado == 'Aprobado':
        evaluacion.estado = Evaluacion.ESTADO_APROBADO
    elif solicitud.estado == 'Rechazado':
        evaluacion.estado = Evaluacion.ESTADO_RECHAZADO
    else:
        evaluacion.estado = Evaluacion.ESTADO_EVALUADO if postulante.ficha_completada else Evaluacion.ESTADO_PENDIENTE
    evaluacion.save()

    return solicitud


class PanelPostulanteView(LoginRequeridoMixin, TemplateView):
    template_name = 'postulantes/panel.html'

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and hasattr(request.user, 'perfil'):
            if request.user.perfil.es_evaluador():
                return redirect('evaluaciones:lista')
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        postulante = getattr(user, 'postulante', None)
        ctx['postulante'] = postulante

        if postulante:
            estado = postulante.get_estado_postulacion()
            ctx['tiene_ficha_socioeconomica'] = estado['ficha_completa']
            ctx['solicitud_beca'] = getattr(postulante, 'solicitud_beca', None)
            ctx['progreso'] = estado['progreso']
            ctx['estado_postulacion'] = estado
        else:
            ctx['tiene_ficha_socioeconomica'] = False
            ctx['progreso'] = 0
            ctx['estado_postulacion'] = {
                'ficha_completa': False,
                'tiene_materias': False,
                'certificado_subido': False,
                'progreso': 0,
                'is_completa': False,
                'estado': 'incompleta',
                'estado_display': 'Postulación en Proceso'
            }

        return ctx


class FichaSocioeconomicaView(PostulanteRequeridoMixin, TemplateView):
    template_name = 'postulantes/ficha_socioeconomica.html'

    def _get_postulante(self):
        return get_object_or_404(Postulante, user=self.request.user)

    def get(self, request, *args, **kwargs):
        postulante = self._get_postulante()
        if postulante.ficha_completada:
            messages.warning(request, 'Tu postulación ya ha sido enviada y no puede ser modificada.')
            return redirect('postulantes:panel')
        ficha = getattr(postulante, 'ficha_socioeconomica', None)
        academicos = getattr(postulante, 'datos_academicos', None)
        
        form_ficha = FichaSocioeconomicaForm(instance=ficha)
        form_acad = DatosAcademicosForm(instance=academicos)
        formset = MiembroFamiliarFormSet(instance=ficha)

        # Deserializar dirección e infraestructura extra de JSON
        import json
        direccion_extra = {}
        if postulante.direccion:
            try:
                direccion_extra = json.loads(postulante.direccion)
            except Exception:
                # Si hay datos legados que no son JSON, los ponemos en el campo calle
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
            'puede_editar': True,  # Permite editar siempre como solicita el usuario
            'direccion_extra': direccion_extra,
        })

    def post(self, request, *args, **kwargs):
        postulante = self._get_postulante()
        if postulante.ficha_completada:
            messages.error(request, 'Tu postulación ya ha sido enviada y no puede ser modificada.')
            return redirect('postulantes:panel')
        ficha = getattr(postulante, 'ficha_socioeconomica', None)
        academicos = getattr(postulante, 'datos_academicos', None)
        
        form_ficha = FichaSocioeconomicaForm(request.POST, request.FILES, instance=ficha)
        form_acad = DatosAcademicosForm(instance=academicos)
        formset = MiembroFamiliarFormSet(request.POST, instance=ficha)
        
        if form_ficha.is_valid() and formset.is_valid():
            with transaction.atomic():
                # Guardar Ficha Socioeconómica
                ficha_obj = form_ficha.save(commit=False)
                ficha_obj.postulante = postulante

                # Subir archivos a Cloudinary secuencialmente con pausas para evitar errores de "Slow Down, Out of Processing Capacity"
                import time
                file_fields = [
                    'archivo_boleta_inscripcion', 'archivo_historico_academico',
                    'archivo_carnet_identidad', 'archivo_carnet_identidad_reverso',
                    'archivo_analisis_medicos', 'doc_ocupacion', 'doc_ingresos', 'doc_vivienda'
                ]
                
                for field_name in file_fields:
                    file_obj = request.FILES.get(field_name)
                    if file_obj:
                        field_attr = getattr(ficha_obj, field_name)
                        max_retries = 3
                        for attempt in range(max_retries):
                            try:
                                field_attr.save(file_obj.name, file_obj, save=False)
                                time.sleep(1.5)  # Pausa entre subidas
                                break
                            except Exception as e:
                                if ("Slow Down" in str(e) or "Capacity" in str(e)) and attempt < max_retries - 1:
                                    time.sleep(4)  # Pausa más larga si hay rate limit
                                    continue
                                raise e

                ficha_obj.save()

                # Guardar Miembros Familiares
                formset.instance = ficha_obj
                formset.save()

                # Guardar Carrera si se envió en el formulario
                carrera_val = request.POST.get('carrera')
                if carrera_val:
                    postulante.carrera = carrera_val

                # Serializar campos adicionales de la ficha (dirección, ambientes, comentarios) como JSON en dirección
                import json
                direccion_data = {
                    'provincia_residencia': request.POST.get('provincia_residencia', '').strip(),
                    'zona_anillo': request.POST.get('zona_anillo', '').strip(),
                    'barrio': request.POST.get('barrio', '').strip(),
                    'calle': request.POST.get('calle', '').strip(),
                    'cant_dormitorios': request.POST.get('cant_dormitorios', '1').strip(),
                    'cant_banos': request.POST.get('cant_banos', '1').strip(),
                    'cant_comedores': request.POST.get('cant_comedores', '0').strip(),
                    'cant_salas': request.POST.get('cant_salas', '0').strip(),
                    'cant_patios': request.POST.get('cant_patios', '0').strip(),
                    'comentarios': request.POST.get('comentarios', '').strip(),
                }
                postulante.direccion = json.dumps(direccion_data)
                postulante.save()

                # Calcular y guardar puntaje
                calcular_y_guardar_puntaje(postulante)

            messages.success(request, '¡Ficha Socioeconómica guardada exitosamente!')
            return redirect('postulantes:panel')

        # Reconstruir direccion_extra y carrera a partir de los datos enviados en POST
        # para que el usuario no pierda su información si el formulario falla validación
        direccion_extra = {
            'provincia_residencia': request.POST.get('provincia_residencia', '').strip(),
            'zona_anillo': request.POST.get('zona_anillo', '').strip(),
            'barrio': request.POST.get('barrio', '').strip(),
            'calle': request.POST.get('calle', '').strip(),
            'cant_dormitorios': request.POST.get('cant_dormitorios', '1').strip(),
            'cant_banos': request.POST.get('cant_banos', '1').strip(),
            'cant_comedores': request.POST.get('cant_comedores', '0').strip(),
            'cant_salas': request.POST.get('cant_salas', '0').strip(),
            'cant_patios': request.POST.get('cant_patios', '0').strip(),
            'comentarios': request.POST.get('comentarios', '').strip(),
        }
        
        carrera_val = request.POST.get('carrera')
        if carrera_val:
            postulante.carrera = carrera_val

        return self.render_to_response({
            'form_ficha': form_ficha,
            'form_acad': form_acad,
            'formset': formset,
            'postulante': postulante,
            'puede_editar': True,
            'direccion_extra': direccion_extra,
        })


class MiPostulacionView(PostulanteRequeridoMixin, TemplateView):
    template_name = 'postulantes/mi_postulacion.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        postulante = get_object_or_404(Postulante, user=self.request.user)
        ctx['postulante'] = postulante
        ctx['solicitud_beca'] = getattr(postulante, 'solicitud_beca', None)
        ctx['datos_academicos'] = getattr(postulante, 'datos_academicos', None)
        ctx['ficha_socioeconomica'] = getattr(postulante, 'ficha_socioeconomica', None)
        ctx['estado_postulacion'] = postulante.get_estado_postulacion()
        return ctx


class RegistroMateriasView(PostulanteRequeridoMixin, TemplateView):
    template_name = 'postulantes/registro_materias.html'

    def get_postulante(self):
        return get_object_or_404(Postulante, user=self.request.user)

    def _get_catalogo(self, postulante):
        """Obtiene las materias del catálogo para la carrera y semestre activo del postulante."""
        from apps.parametros.models import MateriaCatalogo, SemestreRegistro, Carrera
        semestre = SemestreRegistro.get_semestre_activo()
        
        # 1. Normalizar y buscar la carrera en la base de datos
        carrera_nombre = postulante.carrera
        mapeo_nombres = {
            'Ing. en Sistemas': 'Ingeniería en Sistemas',
            'Ingeniería en Sistemas': 'Ing. en Sistemas',
            'Ing. en Agropecuaria': 'Ingeniería Agropecuaria',
            'Ingeniería Agropecuaria': 'Ing. en Agropecuaria',
            'Lic. en Contaduría': 'Contaduría Pública',
            'Contaduría Pública': 'Lic. en Contaduría',
        }
        
        carrera_obj = Carrera.objects.filter(nombre__iexact=carrera_nombre).first()
        if not carrera_obj and carrera_nombre in mapeo_nombres:
            carrera_obj = Carrera.objects.filter(nombre__iexact=mapeo_nombres[carrera_nombre]).first()
            
        if not carrera_obj:
            # Búsqueda difusa para coincidir abreviaciones parciales
            carrera_obj = Carrera.objects.filter(nombre__icontains=carrera_nombre[:10]).first()
            
        if not carrera_obj:
            return MateriaCatalogo.objects.none(), semestre

        # 2. Buscar materias del catálogo
        # Intentar filtrar por carrera y por el periodo académico activo (ej: 2025-2)
        materias = MateriaCatalogo.objects.filter(
            carrera=carrera_obj,
            semestre_academico=semestre,
        ).order_by('orden', 'sigla')
        
        # Fallback: si no hay materias con el periodo activo (por ejemplo, si el administrador configuró 
        # niveles como "SEGUNDO SEMESTRE" en la columna semestre_academico), cargamos todas las materias de esa carrera.
        if not materias.exists():
            materias = MateriaCatalogo.objects.filter(
                carrera=carrera_obj
            ).order_by('semestre_academico', 'orden', 'sigla')
            
        return materias, semestre

    def get(self, request, *args, **kwargs):
        postulante = self.get_postulante()
        if postulante.ficha_completada:
            messages.warning(request, 'Tu postulación ya ha sido enviada y no puede ser modificada.')
            return redirect('postulantes:panel')
        datos_acad, _ = DatosAcademicos.objects.get_or_create(
            postulante=postulante,
            defaults={'ppa': 0.0, 'materias_aprobadas': 0}
        )
        form_acad = RegistroAcademicoForm(instance=datos_acad)
        materias_catalogo, semestre = self._get_catalogo(postulante)

        # Cargar notas previamente guardadas (clave: sigla)
        notas_guardadas = {
            m.sigla: m.nota
            for m in MateriaSemestre.objects.filter(
                postulante=postulante, semestre=semestre
            )
        }
        materias_con_nota = [
            {'catalogo': mc, 'nota': notas_guardadas.get(mc.sigla, '')}
            for mc in materias_catalogo
        ]

        return self.render_to_response({
            'form_acad': form_acad,
            'materias_con_nota': materias_con_nota,
            'semestre_activo': semestre,
            'postulante': postulante,
            'datos_acad': datos_acad,
            'sin_catalogo': not materias_catalogo.exists(),
        })

    def post(self, request, *args, **kwargs):
        postulante = self.get_postulante()
        if postulante.ficha_completada:
            messages.error(request, 'Tu postulación ya ha sido enviada y no puede ser modificada.')
            return redirect('postulantes:panel')
        datos_acad, _ = DatosAcademicos.objects.get_or_create(
            postulante=postulante,
            defaults={'ppa': 0.0, 'materias_aprobadas': 0}
        )
        form_acad = RegistroAcademicoForm(request.POST, request.FILES, instance=datos_acad)
        materias_catalogo, semestre = self._get_catalogo(postulante)

        errores = []

        if form_acad.is_valid():
            with transaction.atomic():
                form_acad.save()

                # Guardar/actualizar notas por cada materia del catálogo
                notas_ingresadas = 0
                for mc in materias_catalogo:
                    nota_str = request.POST.get(f'nota_{mc.pk}', '').strip()
                    if nota_str:
                        try:
                            nota = float(nota_str)
                            if nota < 0 or nota > 100:
                                errores.append(f'{mc.sigla}: la nota debe estar entre 0 y 100.')
                                continue
                            MateriaSemestre.objects.update_or_create(
                                postulante=postulante,
                                sigla=mc.sigla,
                                semestre=semestre,
                                defaults={'nombre': mc.nombre, 'nota': nota},
                            )
                            notas_ingresadas += 1
                        except ValueError:
                            errores.append(f'{mc.sigla}: valor de nota inválido.')
                    else:
                        # Si dejó vacío, eliminar el registro anterior si existía
                        MateriaSemestre.objects.filter(
                            postulante=postulante,
                            sigla=mc.sigla,
                            semestre=semestre,
                        ).delete()

                if notas_ingresadas < 2:
                    errores.append('Debes ingresar la nota de al menos 2 materias para completar el registro.')

                if errores:
                    for err in errores:
                        messages.error(request, err)
                    # Reload para mostrar notas ingresadas
                    notas_guardadas = {
                        m.sigla: m.nota
                        for m in MateriaSemestre.objects.filter(
                            postulante=postulante, semestre=semestre
                        )
                    }
                    materias_con_nota = [
                        {'catalogo': mc, 'nota': notas_guardadas.get(mc.sigla, '')}
                        for mc in materias_catalogo
                    ]
                    return self.render_to_response({
                        'form_acad': form_acad,
                        'materias_con_nota': materias_con_nota,
                        'semestre_activo': semestre,
                        'postulante': postulante,
                        'datos_acad': datos_acad,
                        'sin_catalogo': not materias_catalogo.exists(),
                    })

                # Recalcular puntaje académico
                from .utils import calcular_puntaje_academico
                res = calcular_puntaje_academico(postulante)

            messages.success(request, '✅ Registro exitoso.')
            return redirect('postulantes:panel')

        # form_acad inválido
        notas_guardadas = {
            m.sigla: m.nota
            for m in MateriaSemestre.objects.filter(postulante=postulante, semestre=semestre)
        }
        materias_con_nota = [
            {'catalogo': mc, 'nota': notas_guardadas.get(mc.sigla, '')}
            for mc in materias_catalogo
        ]
        return self.render_to_response({
            'form_acad': form_acad,
            'materias_con_nota': materias_con_nota,
            'semestre_activo': semestre,
            'postulante': postulante,
            'datos_acad': datos_acad,
            'sin_catalogo': not materias_catalogo.exists(),
        })



from django.views import View

class EnviarPostulacionView(PostulanteRequeridoMixin, View):
    def post(self, request, *args, **kwargs):
        postulante = get_object_or_404(Postulante, user=request.user)
        estado = postulante.get_estado_postulacion()
        
        if not estado['is_completa']:
            messages.error(request, 'Debes completar la Ficha Socioeconómica y registrar tus Materias Cursadas antes de enviar.')
            return redirect('postulantes:panel')
            
        with transaction.atomic():
            postulante.ficha_completada = True
            postulante.save()
            solicitud = calcular_y_guardar_puntaje(postulante)
            
        if solicitud.estado == 'Rechazado' and solicitud.rechazado:
            messages.error(request, 'No puedes postular a la beca porque vives o eres del municipio de Vallegrande. Esta beca es exclusiva para personas de fuera de Vallegrande.')
        else:
            messages.success(request, '¡Tu postulación ha sido enviada con éxito! Ahora estás participando en el proceso de asignación de becas.')
        return redirect('postulantes:panel')

