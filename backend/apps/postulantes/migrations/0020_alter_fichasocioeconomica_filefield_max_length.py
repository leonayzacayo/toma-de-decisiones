from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('postulantes', '0019_alter_datosacademicos_certificado_notas_pdf'),
    ]

    operations = [
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='doc_ocupacion',
            field=models.FileField(blank=True, max_length=500, upload_to='fichas/ocupacion/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='doc_ingresos',
            field=models.FileField(blank=True, max_length=500, upload_to='fichas/ingresos/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='doc_vivienda',
            field=models.FileField(blank=True, max_length=500, upload_to='fichas/vivienda/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='archivo_boleta_inscripcion',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to='fichas/boletas/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='archivo_historico_academico',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to='fichas/historicos/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='archivo_carnet_identidad',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to='fichas/identidades/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='archivo_carnet_identidad_reverso',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to='fichas/identidades/'),
        ),
        migrations.AlterField(
            model_name='fichasocioeconomica',
            name='archivo_analisis_medicos',
            field=models.FileField(blank=True, max_length=500, null=True, upload_to='fichas/medicos/'),
        ),
    ]
