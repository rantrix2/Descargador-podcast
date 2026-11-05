"""
Descargador de canales completos de iVoox (v3).

Requisitos:  pip install requests beautifulsoup4 curl_cffi
Compilar:    pyinstaller ivoox_canal_v3.spec
"""
import queue
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# curl_cffi imita la conexión TLS de un Chrome real. iVoox responde 502 a los
# programas que no "parecen" un navegador, así que es MUY recomendable instalarlo.
try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # sin curl_cffi se usa requests con cabeceras de navegador
    cffi_requests = None

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
BASE_URL = "https://www.ivoox.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT = (10, 30)          # (conexión, lectura) en segundos
MAX_PAGINAS = 1000          # tope de seguridad para no entrar en bucle infinito
CHUNK = 64 * 1024           # 64 KB por bloque de descarga
REGISTRO = ".ivoox_descargados.txt"   # ids ya descargados (permite reanudar rápido)

RE_EPISODIO = re.compile(r"_rf_(\d+)_")
RE_PAGINA = re.compile(r"_(\d+)\.html$")


class DescargaCancelada(Exception):
    """Se lanza cuando el usuario pulsa Finalizar."""


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
CABECERAS_NAVEGADOR = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
}
REINTENTABLES = {429, 500, 502, 503, 504}
ERRORES_RED = (requests.RequestException,) + ((cffi_requests.RequestsError,) if cffi_requests else ())


class ClienteHTTP:
    """Sesión HTTP que se hace pasar por Chrome y reintenta los errores temporales."""

    def __init__(self, dormir=time.sleep, reintentos=5):
        self.dormir = dormir                # función de espera (interrumpible desde la UI)
        self.reintentos = reintentos
        self.usa_cffi = cffi_requests is not None
        if self.usa_cffi:
            self.s = cffi_requests.Session(impersonate="chrome")
            self.s.headers.update({"Accept-Language": CABECERAS_NAVEGADOR["Accept-Language"]})
            self.timeout = 30
        else:
            self.s = requests.Session()
            self.s.headers.update(CABECERAS_NAVEGADOR)
            self.timeout = TIMEOUT

    def calentar(self):
        """Visita la portada para obtener cookies, como haría un navegador."""
        try:
            self.get(BASE_URL + "/", reintentos=1).close()
        except ERRORES_RED:
            pass

    def get(self, url, stream=False, reintentos=None):
        intentos = reintentos or self.reintentos
        for intento in range(1, intentos + 1):
            try:
                r = self.s.get(url, stream=stream, timeout=self.timeout, allow_redirects=True)
            except ERRORES_RED:
                if intento == intentos:
                    raise
                self.dormir(3 * intento)
                continue
            if r.status_code in REINTENTABLES and intento < intentos:
                espera = r.headers.get("Retry-After", "")
                r.close()
                self.dormir(int(espera) if espera.isdigit() else 3 * intento + random.uniform(0, 2))
                continue
            return r


def limpiar_nombre(texto, max_len=180):
    """Nombre de archivo válido en Windows: sin caracteres prohibidos ni punto/espacio final."""
    limpio = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", texto or "")
    limpio = re.sub(r"\s+", " ", limpio).strip()
    limpio = limpio[:max_len].rstrip(". ")
    return limpio or "sin_titulo"


def url_pagina(url_canal, n):
    """Devuelve la URL de la página n del canal (…_1.html, …_2.html, …)."""
    url = url_canal.strip().split("#")[0].split("?")[0]
    if RE_PAGINA.search(url):
        return RE_PAGINA.sub(f"_{n}.html", url)
    if url.endswith(".html"):
        return url[:-5] + f"_{n}.html"
    raise ValueError("La URL no parece la de un canal de iVoox (debe terminar en .html).")


def extraer_episodios(html):
    """Lista ordenada y sin duplicados de (audio_id, url_episodio, titulo_provisional)."""
    soup = BeautifulSoup(html, "html.parser")
    vistos, episodios = set(), []
    for a in soup.find_all("a", href=RE_EPISODIO):
        href = a.get("href") or ""
        m = RE_EPISODIO.search(href)
        if not m or m.group(1) in vistos:
            continue
        vistos.add(m.group(1))
        titulo = (a.get("title") or a.get_text(strip=True) or "").strip()
        episodios.append((m.group(1), urljoin(BASE_URL, href), titulo))
    return episodios


def urls_audio(audio_id, html=""):
    """
    Posibles URLs del MP3, por orden de preferencia.

    OJO: el enlace "rf/<id>" que usaba la v2 ahora apunta a go.ivoox.com/rf/...
    (botón "descarga la app"), NO al audio. Por eso la v2 ya no descargaba.
    Las rutas buenas son /listen_mn_<id>_1.mp3 (redirige al MP3 real) y, como
    reserva, las que aparezcan en los datos internos de la página.
    """
    candidatas = [f"{BASE_URL}/listen_mn_{audio_id}_1.mp3"]
    # Rutas .mp3 dentro del JSON de la página (vienen con "/" en lugar de "/")
    texto = html.replace("\\u002F", "/")
    for ruta in re.findall(rf'["\'](/?[^"\'\s<>]*_{audio_id}_[^"\'\s<>]*\.mp3)["\']', texto):
        candidatas.append(urljoin(BASE_URL + "/", ruta))
    candidatas.append(f"{BASE_URL}/_md_{audio_id}_wp_1.mp3")
    return list(dict.fromkeys(candidatas))  # sin duplicados, conservando el orden


def extraer_titulo_episodio(html, audio_id, titulo_provisional):
    """Título real del episodio a partir de su página."""
    soup = BeautifulSoup(html, "html.parser")

    titulo = None
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        titulo = h1.get_text(strip=True)
    if not titulo:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            titulo = og["content"].strip()
    if not titulo and soup.title and soup.title.string:
        titulo = soup.title.string.split(" - ")[0].strip()
    return titulo or titulo_provisional or f"audio_{audio_id}"


# ---------------------------------------------------------------------------
# Lógica de descarga (independiente de la interfaz)
# ---------------------------------------------------------------------------
class DescargadorCanal:
    def __init__(self, url_canal, destino, notificar, stop_event, pause_event, numerar=False):
        self.url_canal = url_canal
        self.destino = Path(destino)
        self.notificar = notificar          # función(tipo, *datos) -> se envía a la cola de la UI
        self.stop_event = stop_event
        self.pause_event = pause_event
        self.numerar = numerar
        self.http = ClienteHTTP(dormir=lambda s: self.esperar(s, s))
        self.stats = {"ok": 0, "omitidos": 0, "errores": 0}

    # --- helpers de control --------------------------------------------------
    def log(self, msg, tag="normal"):
        self.notificar("log", msg, tag)

    def punto_control(self):
        """Lanza DescargaCancelada si se pulsó Finalizar y se bloquea mientras haya pausa."""
        if self.stop_event.is_set():
            raise DescargaCancelada
        while self.pause_event.is_set():
            if self.stop_event.is_set():
                raise DescargaCancelada
            time.sleep(0.3)

    def esperar(self, minimo, maximo):
        """Pausa aleatoria anti-baneo que se puede interrumpir con Finalizar."""
        fin = time.monotonic() + random.uniform(minimo, maximo)
        while time.monotonic() < fin:
            self.punto_control()
            time.sleep(0.2)

    # --- fases ---------------------------------------------------------------
    def listar_episodios(self):
        """Recorre las páginas del canal una sola vez y recoge todos los episodios."""
        episodios, vistos = [], set()
        for n in range(1, MAX_PAGINAS + 1):
            self.punto_control()
            url = url_pagina(self.url_canal, n)
            try:
                r = self.http.get(url)
                estado, html = r.status_code, r.text
                r.close()
            except ERRORES_RED as e:
                if n == 1:
                    raise RuntimeError(f"No se pudo conectar con iVoox: {e}") from e
                self.log(f"⚠️ Error al leer la página {n}: {e}", "error")
                break
            if estado != 200:
                if n == 1:
                    raise RuntimeError(self._diagnostico(estado))
                break  # se acabaron las páginas
            nuevos = [ep for ep in extraer_episodios(html) if ep[0] not in vistos]
            # Sin episodios nuevos = página vacía o iVoox nos devolvió otra vez la 1ª
            if not nuevos:
                break
            vistos.update(ep[0] for ep in nuevos)
            episodios.extend(nuevos)
            self.log(f"📄 Página {n}: {len(nuevos)} episodios (total {len(episodios)})")
            self.esperar(0.5, 1.5)
        return episodios

    def _diagnostico(self, estado):
        if estado == 404:
            return "iVoox dice que esa página no existe (404). Revisa la URL del canal."
        msg = (f"iVoox ha rechazado la petición (código {estado}), aunque la página "
               "se vea bien en el navegador: está bloqueando al programa por no parecer un navegador.")
        if not self.http.usa_cffi:
            msg += ("\n\nSolución: instala curl_cffi con\n    pip install curl_cffi\n"
                    "y vuelve a ejecutar (o recompila el .exe con él instalado).")
        else:
            msg += "\n\nEspera unos minutos y vuelve a intentarlo (puede ser un bloqueo temporal)."
        return msg

    def descargar_audio(self, url, ruta, titulo):
        """Descarga en streaming a un .part y lo renombra al terminar (nunca deja MP3 a medias)."""
        tmp = ruta.with_name(ruta.name + ".part")
        r = self.http.get(url, stream=True)
        try:
            r.raise_for_status()
            tipo = r.headers.get("Content-Type", "").lower()
            if "text/html" in tipo:
                raise ValueError("el servidor devolvió una página web en lugar del audio "
                                 "(episodio privado/de pago o bloqueo temporal)")
            total = int(r.headers.get("Content-Length") or 0)
            bajado, ultimo_pct = 0, -1
            try:
                with open(tmp, "wb") as f:
                    for bloque in r.iter_content(CHUNK):
                        self.punto_control()
                        if not bloque:
                            continue
                        f.write(bloque)
                        bajado += len(bloque)
                        if total:
                            pct = int(bajado * 100 / total)
                            if pct != ultimo_pct:
                                ultimo_pct = pct
                                self.notificar("archivo", pct, f"{bajado/1e6:.1f} / {total/1e6:.1f} MB")
                        else:
                            self.notificar("archivo", None, f"{bajado/1e6:.1f} MB")
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        finally:
            r.close()
        if total and bajado < total:
            tmp.unlink(missing_ok=True)
            raise IOError(f"descarga incompleta ({bajado} de {total} bytes)")
        tmp.replace(ruta)

    def ejecutar(self):
        self.destino.mkdir(parents=True, exist_ok=True)
        registro = self.destino / REGISTRO
        hechos = set(registro.read_text(encoding="utf-8").split()) if registro.exists() else set()

        modo = "curl_cffi (imitando a Chrome)" if self.http.usa_cffi else \
               "requests (sin curl_cffi: iVoox podría bloquearlo)"
        self.log(f"🌐 Conexión: {modo}")
        self.http.calentar()

        self.log("🔍 Buscando episodios del canal...")
        episodios = self.listar_episodios()
        total = len(episodios)
        if not total:
            raise RuntimeError("No se encontraron episodios. Revisa que la URL sea la del canal.")
        self.log(f"🎧 {total} episodios encontrados.\n", "green")

        ancho = len(str(total))
        nombres_usados = set()

        for i, (audio_id, url_ep, titulo_tmp) in enumerate(episodios, start=1):
            self.punto_control()
            self.notificar("total", i, total)

            if audio_id in hechos:
                self.stats["omitidos"] += 1
                continue

            # La página del episodio solo hace falta para el título real; si falla,
            # se sigue con el título del listado (la URL del audio no depende de ella).
            titulo, html_ep = titulo_tmp or f"audio_{audio_id}", ""
            try:
                r_ep = self.http.get(url_ep)
                r_ep.raise_for_status()
                html_ep = r_ep.text
                r_ep.close()
                titulo = extraer_titulo_episodio(html_ep, audio_id, titulo_tmp)
            except ERRORES_RED as e:
                self.log(f"⚠️ [{i}/{total}] No se pudo abrir la ficha ({e}); uso el título del listado")

            nombre = limpiar_nombre(titulo)
            if self.numerar:  # la página lista primero lo más nuevo → el más antiguo es el 1
                nombre = f"{str(total - i + 1).zfill(ancho)} - {nombre}"
            if nombre.lower() in nombres_usados:          # títulos repetidos en el canal
                nombre = f"{nombre} [{audio_id}]"
            nombres_usados.add(nombre.lower())
            ruta = self.destino / f"{nombre}.mp3"

            if ruta.exists():
                self.log(f"⏭️  [{i}/{total}] Ya existe: {titulo}")
                self._marcar_hecho(registro, hechos, audio_id)
                self.stats["omitidos"] += 1
                continue

            self.log(f"⬇️  [{i}/{total}] {titulo}", "green")
            ultimo_error = None
            for url_audio in urls_audio(audio_id, html_ep):
                try:
                    self.descargar_audio(url_audio, ruta, titulo)
                    ultimo_error = None
                    break
                except (requests.RequestException, IOError, ValueError) as e:
                    ultimo_error = e  # se prueba la siguiente URL candidata
            if ultimo_error:
                self.log(f"⚠️ Error al descargar «{titulo}»: {ultimo_error}", "error")
                self.stats["errores"] += 1
                continue

            self._marcar_hecho(registro, hechos, audio_id)
            self.stats["ok"] += 1
            self.log("   ✔️ Completado", "green")
            self.esperar(1.0, 2.5)

    @staticmethod
    def _marcar_hecho(registro, hechos, audio_id):
        hechos.add(audio_id)
        with open(registro, "a", encoding="utf-8") as f:
            f.write(audio_id + "\n")


# ---------------------------------------------------------------------------
# Interfaz gráfica
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Descargador iVoox")
        self.geometry("980x560")
        self.minsize(820, 480)

        self.cola = queue.Queue()          # el hilo de descarga solo escribe aquí
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.hilo = None

        self._construir()
        self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
        self.after(100, self._procesar_cola)

    # --- construcción --------------------------------------------------------
    def _construir(self):
        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        izq = ttk.Frame(main)
        izq.pack(side="left", fill="y", padx=(0, 16))

        ttk.Label(izq, text="Descargador iVoox", font=(None, 14, "bold")).pack(pady=(0, 12))

        ttk.Label(izq, text="URL del canal iVoox:").pack(anchor="w")
        self.url_entrada = ttk.Entry(izq, width=50)
        self.url_entrada.pack(fill="x", pady=(0, 10))

        ttk.Label(izq, text="Directorio de descarga:").pack(anchor="w")
        fila = ttk.Frame(izq)
        fila.pack(fill="x", pady=(0, 10))
        self.directorio_entrada = ttk.Entry(fila, width=40)
        self.directorio_entrada.pack(side="left", fill="x", expand=True)
        ttk.Button(fila, text="Seleccionar", command=self._seleccionar_directorio).pack(side="left", padx=(8, 0))

        ttk.Label(izq, text="Nombre de carpeta destino (opcional):").pack(anchor="w")
        self.carpeta_entrada = ttk.Entry(izq, width=50)
        self.carpeta_entrada.pack(fill="x", pady=(0, 8))

        self.numerar_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(izq, text="Numerar episodios (001 = el más antiguo)",
                        variable=self.numerar_var).pack(anchor="w", pady=(0, 8))

        self.btn_descargar = ttk.Button(izq, text="Descargar ahora", command=self._iniciar)
        self.btn_descargar.pack(fill="x", pady=4)

        ctrl = ttk.Frame(izq)
        ctrl.pack(fill="x", pady=(4, 8))
        self.btn_pausar = ttk.Button(ctrl, text="Pausar", command=self._pausar_o_continuar, state="disabled")
        self.btn_pausar.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.btn_finalizar = ttk.Button(ctrl, text="Finalizar", command=self._finalizar, state="disabled")
        self.btn_finalizar.pack(side="left", fill="x", expand=True)

        ttk.Label(izq, text="Progreso del canal:").pack(anchor="w", pady=(8, 0))
        self.barra_total = ttk.Progressbar(izq, mode="determinate")
        self.barra_total.pack(fill="x")
        self.lbl_total = ttk.Label(izq, text="—")
        self.lbl_total.pack(anchor="w")

        ttk.Label(izq, text="Episodio actual:").pack(anchor="w", pady=(6, 0))
        self.barra_archivo = ttk.Progressbar(izq, mode="determinate")
        self.barra_archivo.pack(fill="x")
        self.lbl_archivo = ttk.Label(izq, text="—")
        self.lbl_archivo.pack(anchor="w")

        ttk.Label(izq, justify="left", text=(
            "Instrucciones:\n"
            "1. Abre el canal en www.ivoox.com\n"
            "2. Copia la URL de la página principal del canal\n"
            "3. Pégala arriba y elige el directorio\n"
            "4. Pulsa 'Descargar ahora'\n"
            "Si lo vuelves a lanzar, continúa donde se quedó."
        )).pack(anchor="w", pady=(12, 0))

        der = ttk.Frame(main)
        der.pack(side="right", fill="both", expand=True)
        ttk.Label(der, text="Estado de descargas:").pack(anchor="w", pady=(0, 4))
        self.log_text = scrolledtext.ScrolledText(
            der, wrap=tk.WORD, font=("Consolas", 9), bg="black", fg="white", insertbackground="white")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_configure("normal", foreground="white")
        self.log_text.tag_configure("green", foreground="#00FF00")
        self.log_text.tag_configure("error", foreground="#FF5555")

        self.url_entrada.bind("<Return>", lambda e: self._iniciar())

    # --- acciones ------------------------------------------------------------
    def _seleccionar_directorio(self):
        carpeta = filedialog.askdirectory(title="Selecciona directorio de descarga")
        if carpeta:
            self.directorio_entrada.delete(0, tk.END)
            self.directorio_entrada.insert(0, carpeta)

    def _log(self, msg, tag="normal"):
        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)

    def _iniciar(self):
        if self.hilo and self.hilo.is_alive():
            return
        url = self.url_entrada.get().strip()
        directorio = self.directorio_entrada.get().strip()
        carpeta = self.carpeta_entrada.get().strip()

        if not url:
            messagebox.showwarning("Falta URL", "Introduce la URL del canal iVoox.")
            return
        try:
            url_pagina(url, 1)
        except ValueError as e:
            messagebox.showwarning("URL no válida", str(e))
            return
        if not directorio:
            messagebox.showwarning("Falta directorio", "Selecciona dónde guardar los audios.")
            return

        if not carpeta:
            # p. ej. "podcast-mi-canal_sq_f11234_1.html" -> "podcast-mi-canal"
            ultimo = url.rstrip("/").split("/")[-1]
            carpeta = re.sub(r"(_sq_f\d+)?(_\d+)?\.html$", "", ultimo) or "iVoox_Canal"
        destino = Path(directorio) / limpiar_nombre(carpeta)

        self.stop_event.clear()
        self.pause_event.clear()
        self.log_text.delete("1.0", tk.END)
        self._log(f"📁 Destino: {destino}")
        self.barra_total["value"] = 0
        self.barra_archivo["value"] = 0
        self.lbl_total.config(text="—")
        self.lbl_archivo.config(text="—")
        self._estado_botones(descargando=True)

        descargador = DescargadorCanal(
            url, destino, lambda *d: self.cola.put(d),
            self.stop_event, self.pause_event, numerar=self.numerar_var.get())

        def trabajo():
            cancelado, error = False, None
            try:
                descargador.ejecutar()
            except DescargaCancelada:
                cancelado = True
            except Exception as e:  # cualquier fallo inesperado se muestra en la UI
                error = e
            self.cola.put(("fin", descargador.stats, str(destino), cancelado, error))

        self.hilo = threading.Thread(target=trabajo, daemon=True)
        self.hilo.start()

    def _pausar_o_continuar(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.btn_pausar.config(text="Pausar")
            self._log("▶️ Continuando...", "green")
        else:
            self.pause_event.set()
            self.btn_pausar.config(text="Continuar")
            self._log("⏸️ Pausado. (Si la pausa es muy larga, el episodio en curso puede "
                      "fallar y se reintentará en la próxima ejecución)")

    def _finalizar(self):
        self.stop_event.set()
        self.btn_pausar.config(state="disabled")
        self.btn_finalizar.config(state="disabled")
        self._log("🛑 Finalizando...", "error")

    def _estado_botones(self, descargando):
        self.btn_descargar.config(state="disabled" if descargando else "normal")
        self.btn_pausar.config(state="normal" if descargando else "disabled", text="Pausar")
        self.btn_finalizar.config(state="normal" if descargando else "disabled")

    def _al_cerrar(self):
        if self.hilo and self.hilo.is_alive():
            if not messagebox.askyesno("Salir", "Hay una descarga en curso. ¿Cerrar de todas formas?"):
                return
            self.stop_event.set()
        self.destroy()

    # --- comunicación con el hilo (solo el hilo principal toca Tk) -----------
    def _procesar_cola(self):
        try:
            while True:
                tipo, *datos = self.cola.get_nowait()
                if tipo == "log":
                    self._log(*datos)
                elif tipo == "total":
                    i, total = datos
                    self.barra_total.config(maximum=total, value=i)
                    self.lbl_total.config(text=f"{i} de {total}")
                    self.barra_archivo["value"] = 0
                elif tipo == "archivo":
                    pct, texto = datos
                    if pct is None:
                        self.barra_archivo.config(mode="indeterminate")
                    else:
                        self.barra_archivo.config(mode="determinate", value=pct)
                    self.lbl_archivo.config(text=texto)
                elif tipo == "fin":
                    self._terminar(*datos)
        except queue.Empty:
            pass
        self.after(100, self._procesar_cola)

    def _terminar(self, stats, destino, cancelado, error):
        self._estado_botones(descargando=False)
        resumen = (f"Descargados: {stats['ok']}   Omitidos: {stats['omitidos']}   "
                   f"Errores: {stats['errores']}")
        if error:
            self._log(f"❌ {error}", "error")
            messagebox.showerror("No se pudo descargar", str(error))
        elif cancelado:
            self._log(f"🛑 Descarga detenida por el usuario.\n{resumen}", "error")
        else:
            self._log(f"\n✔️ Descarga completada.\n{resumen}", "green")
            messagebox.showinfo("Descarga finalizada", f"{resumen}\n\nCarpeta:\n{destino}")


if __name__ == "__main__":
    App().mainloop()
