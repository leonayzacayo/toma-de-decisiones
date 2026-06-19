from django.contrib import admin
from .models import (
    ParametroBeca, RangoPPS, RangoMaterias, OpcionSocioeconomica, ReglaDesempate,
    Carrera, MateriaCatalogo, SemestreRegistro,
)


@admin.register(ParametroBeca)
class ParametroBecaAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'valor', 'vigente', 'modificado_por', 'fecha_modificacion')
    list_filter = ('vigente',)
    search_fields = ('nombre', 'descripcion')
    readonly_fields = ('fecha_modificacion',)


@admin.register(RangoPPS)
class RangoPPSAdmin(admin.ModelAdmin):
    list_display = ('desde', 'hasta', 'puntaje')
    ordering = ('desde',)


@admin.register(RangoMaterias)
class RangoMateriasAdmin(admin.ModelAdmin):
    list_display = ('desde', 'hasta', 'puntaje')
    ordering = ('desde',)


@admin.register(OpcionSocioeconomica)
class OpcionSocioeconomicaAdmin(admin.ModelAdmin):
    list_display = ('variable', 'opcion_texto', 'puntaje')
    list_filter = ('variable',)
    search_fields = ('opcion_texto',)
    ordering = ('variable', '-puntaje')


@admin.register(ReglaDesempate)
class ReglaDesempateAdmin(admin.ModelAdmin):
    list_display = ('id', 'orden_ejecucion', 'nombre', 'campo_modelo', 'direccion', 'activo')
    list_display_links = ('id', 'nombre')
    list_editable = ('orden_ejecucion', 'activo')
    ordering = ('orden_ejecucion',)


# ── Catálogo de Materias ────────────────────────────────────────

class MateriaCatalogoInline(admin.TabularInline):
    model = MateriaCatalogo
    extra = 3
    fields = ('semestre_academico', 'sigla', 'nombre', 'orden')
    ordering = ('semestre_academico', 'orden', 'sigla')


@admin.register(Carrera)
class CarreraAdmin(admin.ModelAdmin):
    list_display = ('nombre', 'codigo', 'total_materias')
    search_fields = ('nombre',)
    inlines = [MateriaCatalogoInline]

    def total_materias(self, obj):
        return obj.materias_catalogo.count()
    total_materias.short_description = 'Total Materias'


@admin.register(MateriaCatalogo)
class MateriaCatalogoAdmin(admin.ModelAdmin):
    list_display = ('carrera', 'semestre_academico', 'sigla', 'nombre', 'orden')
    list_filter = ('carrera', 'semestre_academico')
    search_fields = ('sigla', 'nombre')
    list_editable = ('orden',)
    ordering = ('carrera', 'semestre_academico', 'orden', 'sigla')


@admin.register(SemestreRegistro)
class SemestreRegistroAdmin(admin.ModelAdmin):
    list_display = ('semestre', 'activo', 'descripcion')
    list_editable = ('activo',)
    help_text = 'Solo debe haber UN semestre activo a la vez. Es el semestre cuyos datos registran los postulantes.'
