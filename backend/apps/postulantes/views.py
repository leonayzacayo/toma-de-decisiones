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
        
        # Mapeo de campos textuales directos
        campos_texto = {
            'dependencia': ficha.dependencia,
            'ocupacion': ficha.ocupacion,
            'rango_ingresos': ficha.rango_ingresos,
            'lugar_residencia': ficha.lugar_residencia,
            'tenencia_vivienda': ficha.tenencia_vivienda,
            'tipo_vivienda': ficha.tipo_vivienda,
            'procedencia': ficha.procedencia,
        }
        
        for var_name, value in campos_texto.items():
            opt = OpcionSocioeconomica.objects.filter(variable=var_name, opcion_texto=value).first()
            if opt:
                p_socioeconomico += opt.puntaje

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

    # Guardar o actualizar SolicitudBeca
    solicitud, _ = SolicitudBeca.objects.get_or_create(postulante=postulante)
    solicitud.puntaje_academico = p_academico
    solicitud.puntaje_socioeconomico = p_socioeconomico
    solicitud.puntaje_total = p_total
    solicitud.estado = "Postulación completada" if postulante.ficha_completada else "Pendiente"
    solicitud.save()

    # Sincronizar con el modelo legado Evaluacion
    from apps.evaluaciones.models import Evaluacion
    evaluacion, _ = Evaluacion.objects.get_or_create(postulante=postulante)
    evaluacion.puntaje_academico = p_academico
    evaluacion.puntaje_socioeconomico = p_socioeconomico
    evaluacion.puntaje_total = p_total
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
        ficha = getattr(postulante, 'ficha_socioeconomica', None)
        academicos = getattr(postulante, 'datos_academicos', None)
        
        form_ficha = FichaSocioeconomicaForm(instance=ficha)
        form_acad = DatosAcademicosForm(instance=academicos)
        formset = MiembroFamiliarFormSet(instance=ficha)
        
        return self.render_to_response({
            'form_ficha': form_ficha,
            'form_acad': form_acad,
            'formset': formset,
            'postulante': postulante,
            'puede_editar': True,  # Permite editar siempre como solicita el usuario
        })

    def post(self, request, *args, **kwargs):
        postulante = self._get_postulante()
        ficha = getattr(postulante, 'ficha_socioeconomica', None)
        academicos = getattr(postulante, 'datos_academicos', None)
        
        form_ficha = FichaSocioeconomicaForm(request.POST, request.FILES, instance=ficha)
        form_acad = DatosAcademicosForm(instance=academicos)
        formset = MiembroFamiliarFormSet(request.POST, instance=ficha)
        
        if form_ficha.is_valid():
            with transaction.atomic():
                # Guardar Ficha Socioeconómica
                ficha_obj = form_ficha.save(commit=False)
                ficha_obj.postulante = postulante
                ficha_obj.save()

                # Guardar Miembros Familiares
                formset.instance = ficha_obj
                if formset.is_valid():
                    formset.save()

                # No marcamos la ficha como completada automáticamente aquí.
                # Se marcará cuando el postulante haga clic en "Enviar Postulación".

                # Guardar Carrera si se envió en el formulario
                carrera_val = request.POST.get('carrera')
                if carrera_val:
                    postulante.carrera = carrera_val
                    postulante.save()

                # Calcular y guardar puntaje
                calcular_y_guardar_puntaje(postulante)

            messages.success(request, '¡Ficha Socioeconómica guardada exitosamente!')
            return redirect('postulantes:mi_postulacion')

        return self.render_to_response({
            'form_ficha': form_ficha,
            'form_acad': form_acad,
            'formset': formset,
            'postulante': postulante,
            'puede_editar': True,
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
        from apps.parametros.models import MateriaCatalogo, SemestreRegistro
        semestre = SemestreRegistro.get_semestre_activo()
        
        # Mapeo de compatibilidad para nombres abreviados o desalineados
        carrera_nombre = postulante.carrera
        mapeo_nombres = {
            'Ing. en Sistemas': 'Ingeniería en Sistemas',
            'Ing. en Agropecuaria': 'Ingeniería Agropecuaria',
            'Lic. en Contaduría': 'Contaduría Pública',
        }
        carrera_nombre = mapeo_nombres.get(carrera_nombre, carrera_nombre)

        materias = MateriaCatalogo.objects.filter(
            carrera__nombre__iexact=carrera_nombre,
            semestre_academico=semestre,
        ).order_by('orden', 'sigla')
        return materias, semestre

    def get(self, request, *args, **kwargs):
        postulante = self.get_postulante()
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

            messages.success(
                request,
                f'✅ Registro académico guardado. '
                f'PPA: {res["promedio"]:.2f} | '
                f'Aprobadas: {res["aprobadas"]} materias | '
                f'Puntaje académico: {res["total"]:.1f} pts.'
            )
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
            calcular_y_guardar_puntaje(postulante)
            
        messages.success(request, '¡Tu postulación ha sido enviada con éxito! Ahora estás participando en el proceso de asignación de becas.')
        return redirect('postulantes:panel')

