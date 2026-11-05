[README.md](https://github.com/user-attachments/files/34419064/README.md)
# Descargador de canales de iVoox

Aplicación de escritorio (Windows) para descargar **todos los episodios de un canal de iVoox** de una sola vez, con interfaz gráfica.

## Características

- Descarga el canal completo recorriendo todas sus páginas.
- **Reanuda** donde se quedó: si lo vuelves a lanzar, se salta los episodios ya descargados.
- Botones de **Pausar / Continuar / Finalizar**.
- Barras de progreso del canal y del episodio actual, y resumen final (descargados, omitidos, errores).
- Opción para **numerar** los episodios (`001` = el más antiguo).
- Usa el título real de cada episodio como nombre de archivo.
- Nunca deja MP3 a medias: descarga a un `.part` y lo renombra al terminar.
- Reintentos automáticos ante errores temporales del servidor.

## Requisitos

- Python 3.10 o superior
- Dependencias:

```bash
pip install -r requirements.txt
```

> **Importante:** `curl_cffi` hace que el programa se conecte como un navegador Chrome.
> Sin ella, iVoox suele rechazar las peticiones con un error **502**.

## Uso

```bash
python ivoox_canal_v3.py
```

1. Abre el canal en [www.ivoox.com](https://www.ivoox.com) y copia la URL de su página principal,
   por ejemplo: `https://www.ivoox.com/podcast-nombre-del-canal_sq_f1234567_1.html`
2. Pégala en el programa.
3. Elige el directorio de descarga (y, si quieres, el nombre de la carpeta).
4. Pulsa **Descargar ahora**.

La primera línea del registro indica el tipo de conexión. Debe decir
`🌐 Conexión: curl_cffi (imitando a Chrome)`.

## Crear el ejecutable (.exe)

```bash
pip install pyinstaller
pyinstaller ivoox_canal_v3.spec
```

El ejecutable aparece en la carpeta `dist/`. El `.spec` ya incluye la librería nativa de `curl_cffi`.

## Problemas frecuentes

| Mensaje | Causa / solución |
|---|---|
| `iVoox ha rechazado la petición (código 502)` | iVoox bloquea al programa. Instala `curl_cffi` (o recompila el `.exe` con ella) y vuelve a probar pasados unos minutos. |
| `el servidor devolvió una página web en lugar del audio` | Episodio de pago, privado o un bloqueo temporal. |
| `No se encontraron episodios` | La URL no es la de la página principal del canal. |

## Aviso

Proyecto para **uso personal**, para escuchar sin conexión episodios de acceso público.
No salta contenido de pago ni protecciones de iVoox. Respeta los derechos de los autores
y las condiciones de uso de iVoox.

Si iVoox cambia su web, el programa puede dejar de funcionar hasta que se actualice.
