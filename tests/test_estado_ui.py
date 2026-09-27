import unittest
import subprocess
import sys
import inspect
from dataclasses import FrozenInstanceError

from parlar.estado_ui import ENVOLVENTE_REPOSO, EstadoInterfaz, EstadoUI
from parlar.indicador import (
    ALTO_INDICADOR,
    ALTURA_MAXIMA,
    ALTURA_REPOSO,
    ANCHO_BARRA,
    ANCHO_INDICADOR,
    BucleSinUI,
    CANTIDAD_BARRAS,
    COEFICIENTE_PICO_CAIDA,
    COEFICIENTE_PICO_SUBIDA,
    GAP_BARRAS,
    INTERVALO_REPOSO_MS,
    INTERVALO_ANIMACION_MS,
    Indicador,
    MARGEN_INFERIOR,
    PADDING_HORIZONTAL,
    PASO_BARRAS,
    PICO_VISUAL_MINIMO,
    PISO_RUIDO_ENTRADA,
    PISO_RUIDO_SALIDA,
    actualizar_pico_visual,
    calcular_alturas_escucha,
    calcular_alturas_procesamiento,
    calcular_envelope_espacial,
    calcular_frame_waveform,
    calcular_intervalo_sondeo,
    calcular_posicion,
    comprimir_nivel_visual,
    normalizar_nivel_adaptativo,
    normalizar_forma_local,
    procesar_nivel_visual,
    suavizar_forma_barras,
    variacion_temporal,
)


class PruebasEstadoInterfaz(unittest.TestCase):
    def test_estado_inicial(self):
        estado = EstadoInterfaz()

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(),
        )

    def test_operativo_preserva_continuo(self):
        estado = EstadoInterfaz()

        estado.fijar_continuo(True)
        estado.fijar_operativo("recording")

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(
                generacion=0,
                generacion_valida=True,
                capturando=True,
                continuo_activo=True,
            ),
        )

    def test_continuo_preserva_operativo(self):
        estado = EstadoInterfaz()

        estado.fijar_operativo("transcribing")
        estado.fijar_continuo(True)

        self.assertEqual(
            estado.snapshot(),
            EstadoUI(
                generacion=0,
                generacion_valida=True,
                trabajos_pendientes=1,
                continuo_activo=True,
            ),
        )

    def test_snapshot_es_inmutable(self):
        estado = EstadoInterfaz().snapshot()

        with self.assertRaises(FrozenInstanceError):
            estado.operativo = "recording"

    def test_nivel_visual_es_thread_safe_escalar_y_preserva_estado(self):
        estado = EstadoInterfaz()
        estado.fijar_operativo("recording")
        estado.fijar_presionado(True)

        estado.fijar_nivel_visual(0.42)

        snapshot = estado.snapshot()
        self.assertEqual(snapshot.nivel_visual, 0.42)
        self.assertEqual(snapshot.operativo, "recording")
        self.assertTrue(snapshot.presionado)

    def test_nivel_visual_clampa_entradas_invalidas(self):
        estado = EstadoInterfaz()
        for entrada, esperado in (
            (-1, 0.0), (2, 1.0), (float("nan"), 0.0), (None, 0.0),
        ):
            with self.subTest(entrada=entrada):
                estado.fijar_nivel_visual(entrada)
                self.assertEqual(estado.snapshot().nivel_visual, esperado)

    def test_descriptor_visual_es_inmutable_clampeado_y_atomico(self):
        estado = EstadoInterfaz()
        forma = tuple([-1.0, 2.0] + [0.25] * 15)

        estado.fijar_descriptor_visual(0.42, forma)

        snapshot = estado.snapshot()
        self.assertEqual(snapshot.nivel_visual, 0.42)
        self.assertIs(type(snapshot.envolvente_visual), tuple)
        self.assertEqual(len(snapshot.envolvente_visual), 17)
        self.assertEqual(snapshot.envolvente_visual[:2], (0.0, 1.0))

    def test_descriptor_reemplaza_snapshot_sin_acumular_historial(self):
        estado = EstadoInterfaz()
        primera = tuple(indice / 17 for indice in range(17))
        segunda = tuple(reversed(primera))

        estado.fijar_descriptor_visual(0.2, primera)
        snapshot_primero = estado.snapshot()
        estado.fijar_descriptor_visual(0.4, segunda)
        snapshot_segundo = estado.snapshot()

        self.assertEqual(snapshot_primero.envolvente_visual, primera)
        self.assertEqual(snapshot_segundo.envolvente_visual, segunda)
        self.assertEqual(snapshot_segundo.nivel_visual, 0.4)

    def test_descriptor_invalido_vuelve_a_reposo_seguro(self):
        estado = EstadoInterfaz()

        estado.fijar_descriptor_visual(0.2, (1.0, 0.5))

        self.assertEqual(
            estado.snapshot().envolvente_visual,
            ENVOLVENTE_REPOSO,
        )


class PruebasBucleSinUI(unittest.TestCase):
    def test_expone_el_mismo_contrato_de_estado(self):
        ui = BucleSinUI()

        ui.fijar_estado("error")
        ui.fijar_continuo(True)

        self.assertEqual(
            ui.snapshot_ui(),
            EstadoUI(
                generacion=0,
                error_visual=True,
                continuo_activo=True,
            ),
        )

    def test_conserva_contrato_completo_de_presentacion(self):
        ui = BucleSinUI()

        ui.fijar_estado("recording")
        ui.fijar_continuo(True)
        ui.fijar_presionado(True)

        self.assertEqual(
            ui.snapshot_ui(),
            EstadoUI(
                generacion=0,
                generacion_valida=True,
                capturando=True,
                continuo_activo=True,
                presionado=True,
            ),
        )


class PruebasWaveform(unittest.TestCase):
    def test_frame_es_determinista(self):
        primero = calcular_frame_waveform("recording", fase=3)
        segundo = calcular_frame_waveform("recording", fase=3)

        self.assertEqual(primero, segundo)

    def test_barras_y_geometria_tienen_dimensiones_validas(self):
        for estado in ("idle", "recording", "transcribing", "error"):
            with self.subTest(estado=estado):
                frame = calcular_frame_waveform(estado, fase=5)
                self.assertEqual(len(frame.alturas), CANTIDAD_BARRAS)
                self.assertTrue(all(
                    isinstance(altura, int)
                    and ALTURA_REPOSO <= altura <= ALTURA_MAXIMA
                    for altura in frame.alturas
                ))

        self.assertEqual(
            calcular_posicion(1920, 1080),
            ((1920 - ANCHO_INDICADOR) // 2,
             1080 - ALTO_INDICADOR - MARGEN_INFERIOR),
        )

    def test_recording_y_error_tienen_frames_diferentes(self):
        recording = calcular_frame_waveform("recording", fase=2)
        error = calcular_frame_waveform("error", fase=2)

        self.assertNotEqual(recording.alturas, error.alturas)
        self.assertNotEqual(recording.color, error.color)

    def test_error_es_estatico_entre_fases(self):
        primero = calcular_frame_waveform("error", fase=0)
        despues = calcular_frame_waveform("error", fase=137)

        self.assertEqual(primero, despues)

    def test_calculo_visual_no_modifica_visibilidad(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)
        antes = estado.snapshot()

        calcular_frame_waveform(antes.operativo, fase=4)

        despues = estado.snapshot()
        self.assertEqual(despues, antes)
        self.assertTrue(despues.visible)

    def test_nivel_cero_produce_waveform_plana(self):
        frame = calcular_frame_waveform("recording", fase=0, nivel_visual=0.0)

        self.assertEqual(
            frame.alturas,
            (ALTURA_REPOSO,) * CANTIDAD_BARRAS,
        )

    def test_ruido_bajo_el_piso_permanece_practicamente_plano(self):
        procesado = procesar_nivel_visual(PISO_RUIDO_SALIDA / 2)
        frame = calcular_frame_waveform(
            "recording", fase=2, nivel_visual=procesado.nivel)

        self.assertEqual(procesado.nivel, 0.0)
        self.assertLessEqual(max(frame.alturas), ALTURA_REPOSO)

    def test_voz_supera_noise_floor_y_no_iguala_todas_las_barras(self):
        procesado = procesar_nivel_visual(0.12)
        frame = calcular_frame_waveform(
            "recording", fase=1, nivel_visual=procesado.nivel)

        self.assertTrue(procesado.umbral_activo)
        self.assertGreater(max(frame.alturas), ALTURA_REPOSO)
        self.assertGreater(len(set(frame.alturas)), 1)

    def test_ataque_es_mas_rapido_que_caida(self):
        ataque = procesar_nivel_visual(0.18, 0.0, False)
        caida = procesar_nivel_visual(0.0, ataque.nivel, True)

        self.assertGreaterEqual(ataque.nivel, 0.5)
        self.assertGreater(caida.nivel, 0.0)
        self.assertLess(ataque.nivel - caida.nivel, ataque.nivel)

    def test_caida_es_gradual_hasta_reposo(self):
        estado = procesar_nivel_visual(0.18)
        niveles = []
        for _ in range(30):
            estado = procesar_nivel_visual(
                0.0, estado.nivel, estado.umbral_activo)
            niveles.append(estado.nivel)

        self.assertTrue(all(
            posterior <= anterior
            for anterior, posterior in zip(niveles, niveles[1:])
        ))
        self.assertEqual(niveles[-1], 0.0)

    def test_histeresis_evitar_oscilar_alrededor_del_umbral(self):
        iniciado = procesar_nivel_visual(PISO_RUIDO_ENTRADA + 0.002)
        intermedio = procesar_nivel_visual(
            (PISO_RUIDO_ENTRADA + PISO_RUIDO_SALIDA) / 2,
            iniciado.nivel,
            iniciado.umbral_activo,
        )
        detenido = procesar_nivel_visual(
            PISO_RUIDO_SALIDA - 0.001,
            intermedio.nivel,
            intermedio.umbral_activo,
        )

        self.assertTrue(iniciado.umbral_activo)
        self.assertTrue(intermedio.umbral_activo)
        self.assertFalse(detenido.umbral_activo)

    def test_nivel_visual_siempre_se_limita_a_cero_uno(self):
        for valor in (-10.0, 0.5, 10.0, float("nan"), float("inf")):
            with self.subTest(valor=valor):
                resultado = procesar_nivel_visual(valor, valor)
                self.assertGreaterEqual(resultado.nivel, 0.0)
                self.assertLessEqual(resultado.nivel, 1.0)

    def test_transcribing_y_error_conservan_frames_validos(self):
        transcribiendo = calcular_frame_waveform(
            "transcribing", fase=2, nivel_visual=1.0)
        error = calcular_frame_waveform("error", fase=99, nivel_visual=1.0)

        self.assertTrue(all(ALTURA_REPOSO <= altura <= ALTURA_MAXIMA
                            for altura in transcribiendo.alturas))
        self.assertEqual(error, calcular_frame_waveform("error", fase=0))


class PruebasRendererFinal(unittest.TestCase):
    def test_son_exactamente_diecisiete_alturas(self):
        for estado in ("idle", "recording", "transcribing", "error"):
            with self.subTest(estado=estado):
                frame = calcular_frame_waveform(
                    estado, fase=7, nivel_visual=0.45)
                self.assertEqual(len(frame.alturas), 17)

    def test_voz_baja_supera_reposo_y_normal_supera_baja(self):
        reposo = calcular_alturas_escucha(0.0, 3.0)
        baja = calcular_alturas_escucha(0.05, 3.0)
        normal = calcular_alturas_escucha(0.30, 3.0)

        self.assertGreater(max(baja), max(reposo))
        self.assertGreater(max(normal), max(baja))

    def test_voz_fuerte_se_comprime_sin_exceder_maximo(self):
        fuerte = calcular_alturas_escucha(1.0, 4.0)

        self.assertLessEqual(max(fuerte), ALTURA_MAXIMA)
        self.assertTrue(all(altura <= ALTO_INDICADOR for altura in fuerte))

    def test_compresion_clampa_y_devuelve_cero_uno(self):
        casos = (-10.0, 0.0, 0.02, 0.5, 1.0, 10.0, float("nan"))
        for nivel in casos:
            with self.subTest(nivel=nivel):
                comprimido = comprimir_nivel_visual(nivel)
                self.assertGreaterEqual(comprimido, 0.0)
                self.assertLessEqual(comprimido, 1.0)
        self.assertEqual(comprimir_nivel_visual(0.0), 0.0)
        self.assertEqual(comprimir_nivel_visual(1.0), 1.0)

    def test_compresion_es_monotona(self):
        entradas = tuple(indice / 100 for indice in range(101))
        salidas = tuple(comprimir_nivel_visual(valor) for valor in entradas)

        self.assertTrue(all(
            posterior >= anterior
            for anterior, posterior in zip(salidas, salidas[1:])
        ))

    def test_envelope_tiene_mas_capacidad_central_que_en_extremos(self):
        envelope = calcular_envelope_espacial()
        promedio_centro = sum(envelope[6:11]) / 5
        extremos = envelope[:3] + envelope[-3:]
        promedio_extremos = sum(extremos) / len(extremos)

        self.assertEqual(len(envelope), CANTIDAD_BARRAS)
        self.assertGreater(promedio_centro, promedio_extremos)

    def test_barras_con_voz_no_son_identicas(self):
        alturas = calcular_alturas_escucha(0.45, 5.0)

        self.assertGreater(len(set(alturas)), 3)

    def test_variacion_temporal_es_determinista(self):
        primera = tuple(
            variacion_temporal(indice, 8.25)
            for indice in range(CANTIDAD_BARRAS)
        )
        segunda = tuple(
            variacion_temporal(indice, 8.25)
            for indice in range(CANTIDAD_BARRAS)
        )

        self.assertEqual(primera, segunda)
        self.assertGreater(len(set(primera)), 3)

    def test_pasos_temporales_pequenos_no_producen_saltos_grandes(self):
        antes = calcular_alturas_escucha(0.55, 10.0)
        despues = calcular_alturas_escucha(0.55, 10.033)

        self.assertLessEqual(
            max(abs(a - b) for a, b in zip(antes, despues)),
            2,
        )

    def test_misma_entrada_y_fase_produce_mismo_resultado(self):
        self.assertEqual(
            calcular_alturas_escucha(0.37, 12.5),
            calcular_alturas_escucha(0.37, 12.5),
        )

    def test_transcribing_es_valido_e_independiente_del_audio(self):
        sin_audio = calcular_frame_waveform(
            "transcribing", fase=9, nivel_visual=0.0)
        con_audio = calcular_frame_waveform(
            "transcribing", fase=9, nivel_visual=1.0)

        self.assertEqual(sin_audio, con_audio)
        self.assertEqual(
            sin_audio.alturas,
            calcular_alturas_procesamiento(9),
        )
        self.assertGreater(len(set(sin_audio.alturas)), 1)

    def test_error_es_estatico_valido_y_distinto_de_procesamiento(self):
        error_inicial = calcular_frame_waveform("error", fase=0)
        error_tardio = calcular_frame_waveform("error", fase=1000)
        procesando = calcular_frame_waveform("transcribing", fase=0)

        self.assertEqual(error_inicial, error_tardio)
        self.assertNotEqual(error_inicial.alturas, procesando.alturas)

    def test_todas_las_alturas_caben_en_canvas(self):
        for nivel in (0.0, 0.02, 0.1, 0.5, 1.0):
            for fase in (0.0, 1.0, 10.0, 100.0):
                with self.subTest(nivel=nivel, fase=fase):
                    alturas = calcular_alturas_escucha(nivel, fase)
                    self.assertTrue(all(
                        ALTURA_REPOSO <= altura <= ALTURA_MAXIMA
                        and altura <= ALTO_INDICADOR
                        for altura in alturas
                    ))


class PruebasGananciaVisualAdaptativa(unittest.TestCase):
    @staticmethod
    def _estabilizar(nivel, frames=8):
        estado = procesar_nivel_visual(nivel)
        for _ in range(frames - 1):
            estado = procesar_nivel_visual(
                nivel,
                estado.nivel,
                estado.umbral_activo,
                estado.pico_visual,
            )
        return estado

    def test_peak_follower_sube_rapido(self):
        actualizado = actualizar_pico_visual(
            0.20, PICO_VISUAL_MINIMO, True)

        self.assertGreater(actualizado, 0.09)
        self.assertGreater(COEFICIENTE_PICO_SUBIDA, 0.4)

    def test_peak_follower_cae_lento(self):
        actualizado = actualizar_pico_visual(0.02, 0.20, True)

        self.assertGreater(actualizado, 0.19)
        self.assertLess(COEFICIENTE_PICO_CAIDA, 0.01)

    def test_ruido_no_eleva_ganancia(self):
        desde_minimo = actualizar_pico_visual(
            PISO_RUIDO_SALIDA / 2,
            PICO_VISUAL_MINIMO,
            False,
        )
        desde_pico = actualizar_pico_visual(
            PISO_RUIDO_SALIDA / 2,
            0.20,
            False,
        )

        self.assertEqual(desde_minimo, PICO_VISUAL_MINIMO)
        self.assertLess(desde_pico, 0.20)

    def test_silencio_despues_de_voz_vuelve_a_reposo(self):
        estado = self._estabilizar(0.08, frames=6)
        for _ in range(60):
            estado = procesar_nivel_visual(
                0.0,
                estado.nivel,
                estado.umbral_activo,
                estado.pico_visual,
            )

        self.assertEqual(estado.nivel, 0.0)
        self.assertEqual(
            calcular_alturas_escucha(estado.nivel, 2.0),
            (ALTURA_REPOSO,) * CANTIDAD_BARRAS,
        )

    def test_rms_cero_cero_tres_produce_amplitud_util(self):
        estado = self._estabilizar(0.03)
        alturas = calcular_alturas_escucha(estado.nivel, 1.0)

        self.assertGreaterEqual(max(alturas), 20)
        self.assertLess(max(alturas), ALTURA_MAXIMA)

    def test_rms_alto_no_clipea_contra_canvas(self):
        estado = self._estabilizar(0.80)
        alturas = calcular_alturas_escucha(estado.nivel, 1.5)

        self.assertTrue(all(
            ALTURA_REPOSO <= altura <= ALTURA_MAXIMA < ALTO_INDICADOR
            for altura in alturas
        ))

    def test_adaptacion_siempre_permanece_en_cero_uno(self):
        pico = PICO_VISUAL_MINIMO
        for nivel in (-10, 0, 0.03, 0.5, 10, float("nan"), float("inf")):
            with self.subTest(nivel=nivel):
                pico = actualizar_pico_visual(nivel, pico, True)
                normalizado = normalizar_nivel_adaptativo(nivel, pico)
                resultado = procesar_nivel_visual(nivel, pico_anterior=pico)
                self.assertGreaterEqual(normalizado, 0.0)
                self.assertLessEqual(normalizado, 1.0)
                self.assertGreaterEqual(resultado.nivel, 0.0)
                self.assertLessEqual(resultado.nivel, 1.0)
                self.assertGreaterEqual(resultado.pico_visual, 0.0)
                self.assertLessEqual(resultado.pico_visual, 1.0)

    def test_listening_es_distinto_de_processing(self):
        listening = calcular_frame_waveform(
            "recording", fase=2.0, nivel_visual=0.65)
        processing = calcular_frame_waveform(
            "transcribing", fase=2.0, nivel_visual=0.65)

        self.assertNotEqual(listening.alturas, processing.alturas)
        self.assertNotEqual(listening.color, processing.color)


class PruebasFormaTemporalReal(unittest.TestCase):
    def test_normalizacion_local_conserva_zonas_y_piso_espacial(self):
        cruda = (
            0.01, 0.02, 0.08, 0.16, 0.05, 0.01, 0.03, 0.12, 0.09,
            0.02, 0.01, 0.06, 0.14, 0.05, 0.02, 0.0, 0.01,
        )

        forma = normalizar_forma_local(cruda)

        self.assertEqual(len(forma), 17)
        self.assertTrue(all(0.0 <= valor <= 1.0 for valor in forma))
        self.assertGreater(forma[3], forma[0])
        self.assertGreater(forma[12], forma[10])
        self.assertGreater(forma[15], 0.0)

    def test_rms_bajo_noise_floor_ignora_shape(self):
        forma_extrema = tuple(1.0 if indice % 2 else 0.0
                              for indice in range(17))
        nivel = procesar_nivel_visual(PISO_RUIDO_SALIDA / 2)

        frame = calcular_frame_waveform(
            "recording", 0.0, nivel.nivel, forma_extrema)

        self.assertEqual(frame.alturas, (ALTURA_REPOSO,) * 17)

    def test_voz_real_produce_barras_visiblemente_diferentes(self):
        forma = normalizar_forma_local((
            0.02, 0.03, 0.09, 0.16, 0.07, 0.02, 0.05, 0.13, 0.11,
            0.04, 0.02, 0.08, 0.15, 0.06, 0.03, 0.01, 0.02,
        ))

        alturas = calcular_alturas_escucha(0.72, 1.0, forma)

        self.assertGreater(len(set(alturas)), 6)
        self.assertGreater(max(alturas) - min(alturas), 10)

    def test_smoothing_individual_reduce_salto(self):
        objetivo = tuple(1.0 if indice % 2 else 0.2
                         for indice in range(17))

        primer_frame = suavizar_forma_barras(objetivo)

        self.assertTrue(all(
            0.0 < valor < destino
            for valor, destino in zip(primer_frame, objetivo)
        ))
        self.assertGreater(len(set(primer_frame)), 1)

    def test_attack_por_barra_es_mas_rapido_que_release(self):
        reposo = (0.0,) * 17
        objetivo = (1.0,) * 17

        ataque = suavizar_forma_barras(objetivo, reposo, True)
        release = suavizar_forma_barras(reposo, ataque, True)

        self.assertGreater(ataque[0], 0.5)
        self.assertGreater(release[0], 0.0)
        self.assertLess(ataque[0] - release[0], ataque[0])

    def test_release_por_barra_vuelve_gradualmente_a_cero(self):
        forma = (1.0,) * 17
        muestras = []
        for _ in range(30):
            forma = suavizar_forma_barras(
                ENVOLVENTE_REPOSO, forma, False)
            muestras.append(forma[0])

        self.assertTrue(all(
            posterior <= anterior
            for anterior, posterior in zip(muestras, muestras[1:])
        ))
        self.assertEqual(muestras[-1], 0.0)

    def test_processing_sigue_independiente_del_descriptor(self):
        izquierda = (1.0,) + (0.0,) * 16
        derecha = (0.0,) * 16 + (1.0,)

        primero = calcular_frame_waveform(
            "transcribing", 2.0, 0.0, izquierda)
        segundo = calcular_frame_waveform(
            "transcribing", 2.0, 1.0, derecha)

        self.assertEqual(primero, segundo)


class PruebasGeometriaIndicador(unittest.TestCase):
    def test_bottom_center_usa_dimensiones_finales(self):
        self.assertEqual(
            calcular_posicion(1920, 1080),
            (
                (1920 - ANCHO_INDICADOR) // 2,
                1080 - ALTO_INDICADOR - MARGEN_INFERIOR,
            ),
        )

    def test_barra_gap_padding_y_frame_rate_son_coherentes(self):
        self.assertEqual(PASO_BARRAS, ANCHO_BARRA + GAP_BARRAS)
        self.assertEqual(
            ANCHO_INDICADOR,
            2 * PADDING_HORIZONTAL
            + CANTIDAD_BARRAS * ANCHO_BARRA
            + (CANTIDAD_BARRAS - 1) * GAP_BARRAS,
        )
        self.assertEqual(INTERVALO_ANIMACION_MS, 33)

    def test_ocho_posiciones(self):
        esperadas = {
            "top-left": (12, 12),
            "top-center": (450, 12),
            "top-right": (888, 12),
            "middle-left": (12, 385),
            "middle-right": (888, 385),
            "bottom-left": (12, 758),
            "bottom-center": (450, 758),
            "bottom-right": (888, 758),
        }
        for posicion, esperada in esperadas.items():
            with self.subTest(posicion=posicion):
                self.assertEqual(
                    calcular_posicion(1000, 800, 100, 30, posicion, 12),
                    esperada,
                )

    def test_pantalla_pequena_hace_clamp_sin_coordenadas_invalidas(self):
        for posicion in (
            "top-left", "top-center", "top-right", "middle-left",
            "middle-right", "bottom-left", "bottom-center", "bottom-right",
        ):
            with self.subTest(posicion=posicion):
                self.assertEqual(
                    calcular_posicion(
                        40,
                        20,
                        ANCHO_INDICADOR,
                        ALTO_INDICADOR,
                        posicion,
                        12,
                    ),
                    (0, 0),
                )

    def test_posicion_invalida_falla_explicita(self):
        with self.assertRaisesRegex(ValueError, "inválida"):
            calcular_posicion(1000, 800, 70, 28, "middle-center", 12)

    def test_import_indicador_no_carga_tk_audio_ni_modelos(self):
        codigo = """
import sys
import parlar.indicador
prohibidos = {'tkinter', 'sounddevice', 'faster_whisper', 'ctranslate2'}
assert prohibidos.isdisjoint(sys.modules), prohibidos & set(sys.modules)
"""
        resultado = subprocess.run(
            [sys.executable, "-B", "-c", codigo],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(resultado.returncode, 0, resultado.stderr)



class PruebasContratoBucleSinUI(unittest.TestCase):
    def test_presionado_es_informativo_y_no_inventa_captura(self):
        ui = BucleSinUI()

        ui.fijar_presionado(True)

        snapshot = ui.snapshot_ui()
        self.assertTrue(snapshot.presionado)
        self.assertFalse(snapshot.visible)



class PruebasVisibilidadUI(unittest.TestCase):
    def test_reposo_permanece_oculto_aunque_haya_gesto(self):
        estado = EstadoInterfaz()

        estado.fijar_presionado(True)

        self.assertFalse(estado.snapshot().visible)

    def test_captura_real_hace_visible_listening(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)

        estado.iniciar_captura(1)

        snapshot = estado.snapshot()
        self.assertTrue(snapshot.visible)
        self.assertEqual(snapshot.operativo, "recording")

    def test_continuo_no_inventa_visibilidad_sin_captura(self):
        estado = EstadoInterfaz()
        estado.fijar_continuo(True)

        self.assertFalse(estado.snapshot().visible)

    def test_release_con_trabajo_pasa_directo_a_processing(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)

        estado.cerrar_captura(1, trabajo_pendiente=True)

        snapshot = estado.snapshot()
        self.assertTrue(snapshot.visible)
        self.assertEqual(snapshot.operativo, "transcribing")
        self.assertEqual(snapshot.trabajos_pendientes, 1)

    def test_ultimo_trabajo_oculta(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_trabajo(1)

        estado.finalizar_trabajo(1)

        self.assertFalse(estado.snapshot().visible)

    def test_captura_domina_dos_trabajos_internos(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)
        estado.iniciar_trabajo(1)
        estado.iniciar_trabajo(1)

        snapshot = estado.snapshot()
        self.assertEqual(snapshot.trabajos_pendientes, 2)
        self.assertEqual(snapshot.operativo, "recording")

        estado.finalizar_trabajo(1)
        self.assertEqual(estado.snapshot().operativo, "recording")

    def test_continuo_off_con_pendiente_muestra_processing(self):
        estado = EstadoInterfaz()
        estado.fijar_continuo(True)
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)
        estado.iniciar_trabajo(1)

        estado.fijar_continuo(False)
        estado.cerrar_captura(1)

        self.assertEqual(estado.snapshot().operativo, "transcribing")

    def test_cancel_invalida_trabajo_y_completion_stale_es_inerte(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)
        estado.iniciar_trabajo(1)

        estado.invalidar_generacion(1)
        self.assertFalse(estado.finalizar_trabajo(1))

        snapshot = estado.snapshot()
        self.assertFalse(snapshot.visible)
        self.assertEqual(snapshot.trabajos_pendientes, 0)

    def test_generacion_vieja_no_altera_captura_nueva(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_trabajo(1)
        estado.preparar_generacion(2)
        estado.iniciar_captura(2)

        self.assertFalse(estado.finalizar_trabajo(1))
        self.assertFalse(estado.completar_generacion(1))

        snapshot = estado.snapshot()
        self.assertEqual(snapshot.generacion, 2)
        self.assertEqual(snapshot.operativo, "recording")

    def test_dos_trabajos_terminar_uno_no_oculta_y_ultimo_si(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_trabajo(1)
        estado.iniciar_trabajo(1)

        estado.finalizar_trabajo(1)
        self.assertTrue(estado.snapshot().visible)
        self.assertEqual(estado.snapshot().trabajos_pendientes, 1)

        estado.finalizar_trabajo(1)
        self.assertFalse(estado.snapshot().visible)

    def test_contador_nunca_es_negativo(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)

        self.assertFalse(estado.finalizar_trabajo(1))
        self.assertEqual(estado.snapshot().trabajos_pendientes, 0)

    def test_error_es_temporal_y_perceptible(self):
        class Reloj:
            ahora = 10.0

            def __call__(self):
                return self.ahora

        reloj = Reloj()
        estado = EstadoInterfaz(reloj=reloj, duracion_error_s=2.0)
        estado.preparar_generacion(1)
        estado.completar_generacion(1, con_error=True)

        self.assertEqual(estado.snapshot().operativo, "error")
        reloj.ahora = 11.9
        self.assertTrue(estado.snapshot().visible)
        reloj.ahora = 12.0
        self.assertFalse(estado.snapshot().visible)

    def test_error_viejo_no_pisa_sesion_nueva(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.invalidar_generacion(1)
        estado.preparar_generacion(2)
        estado.iniciar_captura(2)

        self.assertFalse(estado.completar_generacion(1, con_error=True))
        self.assertEqual(estado.snapshot().operativo, "recording")

    def test_shutdown_invalida_estado_y_publicaciones_tardias(self):
        estado = EstadoInterfaz()
        estado.preparar_generacion(1)
        estado.iniciar_captura(1)

        estado.cerrar()

        self.assertFalse(estado.snapshot().visible)
        self.assertFalse(estado.iniciar_trabajo(1))

    def test_polling_reposo_es_menos_frecuente_sin_afectar_transicion(self):
        self.assertEqual(
            calcular_intervalo_sondeo(False, False),
            INTERVALO_REPOSO_MS,
        )
        self.assertEqual(
            calcular_intervalo_sondeo(True, False),
            INTERVALO_ANIMACION_MS,
        )
        self.assertEqual(
            calcular_intervalo_sondeo(False, True),
            INTERVALO_ANIMACION_MS,
        )

    def test_overlay_no_expone_bindings_productivos(self):
        parametros = inspect.signature(Indicador).parameters
        fuente = inspect.getsource(Indicador.__init__)

        self.assertNotIn("al_click", parametros)
        self.assertNotIn(".bind(", fuente)
        self.assertFalse(hasattr(Indicador, "_arrastre_inicio"))

    def test_processing_es_tres_pulsos_y_no_waveform_de_voz(self):
        alturas = calcular_alturas_procesamiento(0.5)
        activas = tuple(
            indice for indice, altura in enumerate(alturas)
            if altura > ALTURA_REPOSO
        )

        self.assertTrue(set(activas).issubset({6, 8, 10}))
        self.assertGreater(len(activas), 0)
        self.assertNotEqual(
            alturas,
            calcular_alturas_escucha(0.7, 0.5),
        )

if __name__ == "__main__":
    unittest.main()
