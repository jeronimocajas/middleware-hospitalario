import os
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import Flask, jsonify, request

app = Flask(__name__)

# ---------------------------------------------------------------
# CONFIGURACIÓN (usa los mismos valores de tu V3 si ya los tenías)
# ---------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "clave_super_secreta_taller")
USUARIOS = {"admisiones": "1234"}  # usuario: contraseña


# ---------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------
def normalizar(texto):
    """Mayúsculas y sin tildes: 'Facturación' -> 'FACTURACION'."""
    texto = unicodedata.normalize("NFD", str(texto))
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return texto.strip().upper()


def error(mensaje, codigo, **extra):
    return jsonify({"estado": "error", "error": mensaje, **extra}), codigo


# ---------------------------------------------------------------
# SEGURIDAD JWT
# ---------------------------------------------------------------
def token_requerido(f):
    @wraps(f)
    def decorador(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return error("Acceso no autorizado: falta el token JWT "
                         "(header Authorization: Bearer <token>)", 401)
        token = auth.split(" ", 1)[1].strip()
        try:
            jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return error("Token JWT expirado", 401)
        except jwt.InvalidTokenError:
            return error("Token JWT inválido", 401)
        return f(*args, **kwargs)
    return decorador


# ---------------------------------------------------------------
# VALIDACIÓN + TRANSFORMACIÓN POR DESTINO
# Cada función devuelve (datos_transformados, mensaje_error)
# ---------------------------------------------------------------
def faltantes(datos, requeridos):
    return [c for c in requeridos if c not in datos or datos[c] in (None, "")]


def transformar_laboratorio(d):
    # Acepta "solicitud" (formato nuevo) o "examen" (formato de clase)
    if "solicitud" not in d and "examen" in d:
        d = {**d, "solicitud": d["examen"]}
    falta = faltantes(d, ["paciente", "solicitud"])
    if falta:
        return None, f"Información incompleta, faltan: {', '.join(falta)}"
    return {"id_paciente": d["paciente"], "tipo_examen": d["solicitud"]}, None


def transformar_facturacion(d):
    # AJUSTA estos campos a lo que tu V3 ya enviaba a Facturación
    falta = faltantes(d, ["paciente", "solicitud"])
    if falta:
        return None, f"Información incompleta, faltan: {', '.join(falta)}"
    return {"id_paciente": d["paciente"], "concepto": d["solicitud"]}, None


def transformar_farmacia(d):
    falta = faltantes(d, ["paciente", "solicitud", "cantidad"])
    if falta:
        return None, f"Información incompleta, faltan: {', '.join(falta)}"
    cantidad = d["cantidad"]
    if isinstance(cantidad, bool) or not isinstance(cantidad, int) or cantidad <= 0:
        return None, "El campo 'cantidad' debe ser un entero mayor que 0"
    return {
        "id_paciente": d["paciente"],
        "medicamento": d["solicitud"],
        "unidades": cantidad,
        "requiere_autorizacion": cantidad > 30,   # regla de negocio
    }, None


def transformar_radiologia(d):
    falta = faltantes(d, ["paciente", "solicitud"])
    if "urgente" not in d:
        falta.append("urgente")
    if falta:
        return None, f"Información incompleta, faltan: {', '.join(falta)}"
    if not isinstance(d["urgente"], bool):
        return None, "El campo 'urgente' debe ser true o false"
    return {
        "id_paciente": d["paciente"],
        "tipo_estudio": d["solicitud"],
        "prioridad": "URGENTE" if d["urgente"] else "NORMAL",  # regla de prioridad
    }, None


# ---------------------------------------------------------------
# SISTEMAS DESTINO (simulados)
# ---------------------------------------------------------------
def sistema_laboratorio(d):
    print("==== SISTEMA DE LABORATORIO ====", d)
    return {"sistema": "LABORATORIO", "mensaje": "Examen registrado en Laboratorio"}


def sistema_facturacion(d):
    print("==== SISTEMA DE FACTURACION ====", d)
    return {"sistema": "FACTURACION", "mensaje": "Cargo registrado en Facturación"}


def sistema_farmacia(d):
    print("==== SISTEMA DE FARMACIA ====", d)
    msg = ("Solicitud en espera de autorización"
           if d["requiere_autorizacion"] else "Medicamento despachado")
    return {"sistema": "FARMACIA", "mensaje": msg}


def sistema_radiologia(d):
    print("==== SISTEMA DE RADIOLOGIA ====", d)
    return {"sistema": "RADIOLOGIA",
            "mensaje": f"Estudio programado con prioridad {d['prioridad']}"}


# ---------------------------------------------------------------
# TABLA DE ENRUTAMIENTO: para agregar otro sistema, solo añade una fila
# ---------------------------------------------------------------
RUTAS = {
    "LABORATORIO": (transformar_laboratorio, sistema_laboratorio),
    "FACTURACION": (transformar_facturacion, sistema_facturacion),
    "FARMACIA":    (transformar_farmacia,    sistema_farmacia),
    "RADIOLOGIA":  (transformar_radiologia,  sistema_radiologia),
}


# ---------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------
@app.route("/", methods=["GET"])
def inicio():
    return jsonify({"servicio": "Middleware hospitalario",
                    "destinos": list(RUTAS.keys())})


@app.route("/login", methods=["POST"])
def login():
    datos = request.get_json(silent=True) or {}
    usuario, clave = datos.get("usuario"), datos.get("clave")
    if USUARIOS.get(usuario) != clave or usuario is None:
        return error("Credenciales incorrectas", 401)
    token = jwt.encode(
        {"sub": usuario, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        SECRET_KEY, algorithm="HS256")
    return jsonify({"token": token})


@app.route("/solicitud", methods=["POST"])
@token_requerido
def solicitud():
    datos = request.get_json(silent=True)
    if not isinstance(datos, dict):
        return error("El cuerpo debe ser un JSON válido", 400)

    if "destino" not in datos or not str(datos["destino"]).strip():
        return error("Información incompleta, falta: destino", 400)

    destino = normalizar(datos["destino"])
    if destino not in RUTAS:
        return error(f"Destino '{datos['destino']}' no existe", 400,
                     destinos_validos=list(RUTAS.keys()))

    transformar, enviar = RUTAS[destino]
    transformados, msg_error = transformar(datos)
    if msg_error:
        return error(msg_error, 400, destino=destino)

    respuesta = enviar(transformados)
    return jsonify({
        "estado": "ok",
        "destino": destino,
        "datos_enviados": transformados,
        "respuesta_sistema": respuesta,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
