import os
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv

# Carga las variables del archivo .env (como NUCLEO_URL)
load_dotenv()

app = Flask(__name__)

NUCLEO_URL = os.getenv("NUCLEO_URL", "https://nucleo-tr.onrender.com").rstrip("/")


def respuesta_error(codigo, mensaje, status):
    """Devuelve errores siempre con el formato exigido por la ficha."""
    return jsonify({"error": {"codigo": codigo, "mensaje": mensaje}}), status


def validar_token():
    """
    Reenvía el token recibido al Núcleo (GET /api/nucleo/sesiones/actual).
    Retorna (usuario, None) si es válido, o (None, respuesta_de_error) si falla.
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        return None, respuesta_error(
            "TOKEN_REQUERIDO",
            "Debes enviar un token en el encabezado Authorization",
            401,
        )

    try:
        resp = requests.get(
            f"{NUCLEO_URL}/api/nucleo/sesiones/actual",
            headers={"Authorization": auth_header},
            timeout=30,
        )
    except requests.RequestException:
        return None, respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo conectar con el servicio del núcleo",
            503,
        )

    if resp.status_code == 200:
        return resp.json(), None

    if resp.status_code == 401:
        return None, respuesta_error(
            "TOKEN_INVALIDO",
            "El token no es válido o expiró",
            401,
        )

    return None, respuesta_error(
        "NUCLEO_NO_DISPONIBLE",
        "El núcleo devolvió una respuesta inesperada",
        503,
    )


@app.route("/api/registro/salud", methods=["GET"])
def salud():
    """Ruta pública para comprobar que el Módulo 1 está en línea."""
    return jsonify({
        "estado": "ok",
        "modulo": "M1 - Registro de tutores y animales",
    }), 200


@app.route("/api/registro/sesion", methods=["GET"])
def verificar_sesion():
    """Ruta protegida de prueba: valida el token contra el núcleo."""
    usuario, error = validar_token()
    if error:
        return error

    return jsonify(usuario), 200


@app.errorhandler(404)
def ruta_no_encontrada(_):
    """Asegura que cualquier ruta inexistente responda 404 con el formato estándar."""
    return respuesta_error("RUTA_NO_ENCONTRADA", "La ruta solicitada no existe", 404)


if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)