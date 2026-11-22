import os
from flask import Flask, jsonify, request, render_template
import re
from datetime import datetime, timezone
import requests
from flask import Flask, jsonify, request
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Carga las variables del archivo .env
load_dotenv()

app = Flask(__name__)

NUCLEO_URL = os.getenv("NUCLEO_URL", "https://nucleo-tr.onrender.com").rstrip("/")
MONGO_URI = os.getenv("MONGO_URI")

# --- CONEXIÓN A MONGODB ATLAS ---
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["m1_registro"]

col_tutores = db["tutores"]
col_prohibiciones = db["prohibiciones"]


def inicializar_lista_negra():
    """Asegura que el RUT de prueba con prohibición (RN-04) exista en MongoDB."""
    try:
        col_prohibiciones.update_one(
            {"rut": "9876543-3"},
            {
                "$setOnInsert": {
                    "rut": "9876543-3",
                    "motivo": "Prohibición de tenencia por maltrato animal (RN-04)",
                    "fechaRegistro": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
            },
            upsert=True,
        )
    except PyMongoError as e:
        print(f"Advertencia: no se pudo inicializar la lista negra en MongoDB: {e}")


inicializar_lista_negra()


# --- FUNCIONES DE APOYO ---
def respuesta_error(codigo, mensaje, status):
    """Devuelve errores siempre con el formato exigido por la ficha."""
    return jsonify({"error": {"codigo": codigo, "mensaje": mensaje}}), status


def validar_formato_rut(rut):
    """
    Valida que el RUT tenga formato sin puntos y con guión (ej: 12345678-9)
    y verifica el dígito verificador con el algoritmo Módulo 11.
    """
    if not isinstance(rut, str):
        return False

    rut = rut.strip().upper()
    if not re.match(r"^\d{7,8}-[0-9K]$", rut):
        return False

    cuerpo, dv_ingresado = rut.split("-")
    suma = 0
    multiplicador = 2

    for digito in reversed(cuerpo):
        suma += int(digito) * multiplicador
        multiplicador = 2 if multiplicador == 7 else multiplicador + 1

    resto = 11 - (suma % 11)
    if resto == 11:
        dv_calculado = "0"
    elif resto == 10:
        dv_calculado = "K"
    else:
        dv_calculado = str(resto)

    return dv_ingresado == dv_calculado


def validar_token():
    """
    Reenvía el token recibido al Núcleo (GET /api/nucleo/sesiones/actual).
    Retorna (usuario, auth_header, None) si es válido, o (None, None, error) si falla.
    """
    auth_header = request.headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        return None, None, respuesta_error(
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
        return None, None, respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo conectar con el servicio del núcleo",
            503,
        )

    if resp.status_code == 200:
        return resp.json(), auth_header, None

    if resp.status_code == 401:
        return None, None, respuesta_error(
            "TOKEN_INVALIDO",
            "El token no es válido o expiró",
            401,
        )

    return None, None, respuesta_error(
        "NUCLEO_NO_DISPONIBLE",
        "El núcleo devolvió una respuesta inesperada",
        503,
    )


# --- RUTAS DEL MÓDULO 1 ---
@app.route("/api/registro/salud", methods=["GET"])
def salud():
    """Comprueba que el servidor y la conexión a MongoDB Atlas están activos."""
    try:
        client.admin.command("ping")
        bd_estado = "conectada"
    except PyMongoError:
        bd_estado = "sin_conexion"

    return jsonify({
        "estado": "ok",
        "modulo": "M1 - Registro de tutores y animales",
        "baseDatos": bd_estado,
    }), 200


@app.route("/api/registro/sesion", methods=["GET"])
def verificar_sesion():
    """Ruta de prueba: valida el token contra el núcleo."""
    usuario, _, error = validar_token()
    if error:
        return error
    return jsonify(usuario), 200


@app.route("/api/registro/tutores", methods=["POST"])
def registrar_tutor():
    """
    HU-01: Registrar un tutor.
    Valida token/perfil, formato de RUT, existencia en el Núcleo,
    lista negra interna (RN-04) y guarda fecha en ISO 8601 UTC.
    """
    # 1. Validar sesión contra el Núcleo
    usuario_actual, auth_header, error = validar_token()
    if error:
        return error

    # 2. Leer y validar el JSON recibido (OWASP: validación de entradas)
    datos = request.get_json(silent=True)
    if not datos or not isinstance(datos, dict):
        return respuesta_error("JSON_INVALIDO", "El cuerpo de la petición debe ser un JSON válido", 400)

    rut = str(datos.get("rut", "")).strip().upper()
    comuna = str(datos.get("comuna", "")).strip()
    telefono = str(datos.get("telefono", "")).strip()

    if not rut or not comuna or not telefono:
        return respuesta_error(
            "DATOS_FALTANTES",
            "Los campos rut, comuna y telefono son obligatorios",
            400,
        )

    # 3. Criterio 1: Validar formato y dígito verificador del RUT
    if not validar_formato_rut(rut):
        return respuesta_error(
            "RUT_INVALIDO",
            "El RUT debe venir sin puntos, con guión y dígito verificador válido",
            400,
        )

    # 4. Validar perfil (RN-05): vecino/comunitario solo puede registrar su propio RUT
    perfil = usuario_actual.get("perfil")
    rut_sesion = usuario_actual.get("rut", "").upper()

    if perfil in ("vecino", "comunitario") and rut != rut_sesion:
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Solo puedes registrar tus propios datos de tutor",
            403,
        )

    if perfil not in ("vecino", "comunitario", "funcionario", "jefatura", "admin"):
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "Tu perfil no tiene permisos para registrar tutores",
            403,
        )

    # 5. Consultar al Núcleo para confirmar que la persona existe (sin duplicar nombre ni perfil)
    try:
        resp_usuario = requests.get(
            f"{NUCLEO_URL}/api/nucleo/usuarios/{rut}",
            headers={"Authorization": auth_header},
            timeout=30,
        )
    except requests.RequestException:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo verificar la persona en el núcleo",
            503,
        )

    if resp_usuario.status_code == 404:
        return respuesta_error(
            "USUARIO_NO_ENCONTRADO",
            "El RUT ingresado no existe en el registro del núcleo",
            404,
        )
    if resp_usuario.status_code == 403:
        return respuesta_error(
            "PERFIL_NO_AUTORIZADO",
            "No tienes permiso para consultar este RUT",
            403,
        )
    if resp_usuario.status_code != 200:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "Error al consultar el usuario en el núcleo",
            503,
        )

    datos_nucleo = resp_usuario.json()

    # 6. Criterio 2: Verificar automáticamente contra la lista negra interna (RN-04)
    try:
        en_lista_negra = col_prohibiciones.find_one({"rut": rut}) is not None
        if en_lista_negra or datos_nucleo.get("prohibicionTenencia") is True:
            return respuesta_error(
                "PROHIBICION_TENENCIA",
                "El RUT registra prohibición de tenencia de animales (RN-04)",
                409,
            )

        # Verificar que no esté ya registrado
        if col_tutores.find_one({"rut": rut}):
            return respuesta_error(
                "TUTOR_YA_REGISTRADO",
                "Este RUT ya se encuentra registrado como tutor",
                409,
            )

        # 7. Criterio 3: Almacenar en MongoDB con fecha en ISO 8601 UTC
        # Solo guardamos rut, comuna, telefono y fechaRegistro (no duplicamos nombre ni perfil)
        fecha_registro = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nuevo_tutor = {
            "rut": rut,
            "comuna": comuna,
            "telefono": telefono,
            "fechaRegistro": fecha_registro,
        }
        col_tutores.insert_one(nuevo_tutor)

    except PyMongoError:
        return respuesta_error(
            "ERROR_INTERNO",
            "No se pudo completar el registro en la base de datos",
            500,
        )

    return jsonify({
        "rut": rut,
        "nombre": datos_nucleo.get("nombre"),
        "perfil": datos_nucleo.get("perfil"),
        "comuna": comuna,
        "telefono": telefono,
        "fechaRegistro": fecha_registro,
    }), 201

@app.route("/", methods=["GET"])
def inicio():
    """Sirve la interfaz web del Módulo 1."""
    return render_template("index.html")


@app.route("/api/registro/login", methods=["POST"])
def iniciar_sesion():
    """Puente hacia el Núcleo para iniciar sesión desde el navegador sin problemas de CORS."""
    datos = request.get_json(silent=True)
    if not datos or not datos.get("rut") or not datos.get("clave"):
        return respuesta_error("DATOS_FALTANTES", "Debes ingresar RUT y clave", 400)

    try:
        resp = requests.post(
            f"{NUCLEO_URL}/api/nucleo/sesiones",
            json={"rut": datos["rut"].strip(), "clave": datos["clave"]},
            timeout=30,
        )
    except requests.RequestException:
        return respuesta_error(
            "NUCLEO_NO_DISPONIBLE",
            "No se pudo conectar con el servicio del núcleo",
            503,
        )

    try:
        return jsonify(resp.json()), resp.status_code
    except ValueError:
        return respuesta_error("NUCLEO_NO_DISPONIBLE", "Respuesta inválida del núcleo", 503)

@app.errorhandler(404)
def ruta_no_encontrada(_):
    return respuesta_error("RUTA_NO_ENCONTRADA", "La ruta solicitada no existe", 404)


if __name__ == "__main__":
    puerto = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)