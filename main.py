from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Literal, Dict, Any
from firebase_admin import credentials, firestore, initialize_app
from datetime import datetime, timezone
import os

# --------------------------------------------------
# Configuracion Firebase Admin
# --------------------------------------------------
# Requiere variable de entorno:
# GOOGLE_APPLICATION_CREDENTIALS=/ruta/serviceAccountKey.json
# o adaptar credenciales manuales si prefieres.

if not len(getattr(initialize_app, "_apps", [])):
    try:
        cred = credentials.ApplicationDefault()
        initialize_app(cred)
    except Exception:
        # Fallback si ya estuviera inicializado o si usas entorno gestionado
        try:
            initialize_app()
        except Exception:
            pass


db = firestore.client()

app = FastAPI(title="ND Hoteles Backend", version="1.0.0")

origins = os.getenv("CORS_ORIGINS", "*").split(",")
admin_token = os.getenv("ADMIN_TOKEN", "cambiar-esto")
agent_token = os.getenv("AGENT_TOKEN", "cambiar-esto")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_CREATE_TYPES = {"videollamada", "ayuda", "urgente"}
VALID_CHANNELS = {"kiosko", "telefono"}
VALID_TRANSITIONS = {
    "nuevo": {"en_atencion", "en_proceso", "resuelta", "retorno_inicio"},
    "en_atencion": {"listo_para_entrar", "en_proceso", "resuelta", "retorno_inicio"},
    "listo_para_entrar": {"finalizada", "en_proceso", "resuelta", "retorno_inicio"},
    "en_proceso": {"resuelta", "retorno_inicio", "archivada"},
    "resuelta": {"archivada"},
    "finalizada": {"archivada"},
    "retorno_inicio": {"archivada"},
    "archivada": set(),
}


class CreateAvisoRequest(BaseModel):
    hotel: str = Field(default="Valdemoro", min_length=1, max_length=120)
    punto: str = Field(default="Entrada principal", min_length=1, max_length=120)
    tipo: Literal["videollamada", "ayuda", "urgente"]
    origenCanal: Literal["kiosko", "telefono"] = "kiosko"
    dailyLink: Optional[str] = None
    nombreHuesped: Optional[str] = Field(default="", max_length=150)
    telefonoContacto: Optional[str] = Field(default="", max_length=50)
    anotacionAgente: Optional[str] = Field(default="", max_length=4000)
    creadoManual: bool = False


class UpdateEstadoRequest(BaseModel):
    estado: Literal[
        "nuevo",
        "en_atencion",
        "listo_para_entrar",
        "en_proceso",
        "resuelta",
        "finalizada",
        "retorno_inicio",
        "archivada",
    ]
    resultado: Optional[str] = None
    retornarKiosko: Optional[bool] = None


class UpdateContactoRequest(BaseModel):
    nombreHuesped: str = Field(default="", max_length=150)
    telefonoContacto: str = Field(default="", max_length=50)


class UpdateAnotacionRequest(BaseModel):
    anotacionAgente: str = Field(default="", max_length=4000)


class SetDisponibilidadRequest(BaseModel):
    disponible: bool
    mensaje: Optional[str] = Field(default="")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_agent(x_agent_token: Optional[str]):
    if x_agent_token != agent_token and x_agent_token != admin_token:
        raise HTTPException(status_code=401, detail="Token de agente no valido")


def require_admin(x_admin_token: Optional[str]):
    if x_admin_token != admin_token:
        raise HTTPException(status_code=401, detail="Token de administrador no valido")


def get_aviso_ref(aviso_id: str):
    return db.collection("avisos").document(aviso_id)


def get_aviso_data_or_404(aviso_id: str) -> Dict[str, Any]:
    snap = get_aviso_ref(aviso_id).get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Aviso no encontrado")
    data = snap.to_dict()
    data["id"] = snap.id
    return data


def validate_transition(current_estado: str, next_estado: str):
    current = (current_estado or "nuevo").lower()
    nxt = (next_estado or "").lower()
    allowed = VALID_TRANSITIONS.get(current, set())
    if nxt != current and nxt not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Transicion no permitida: {current} -> {nxt}",
        )


@app.get("/health")
def health():
    return {"ok": True, "service": "nd-hoteles-backend", "time": now_iso()}


@app.get("/disponibilidad")
def get_disponibilidad():
    snap = db.collection("config").document("recepcion").get()
    if not snap.exists:
        return {"disponible": True, "mensaje": "Recepcion disponible"}
    data = snap.to_dict() or {}
    return {
        "disponible": bool(data.get("disponible", True)),
        "mensaje": data.get("mensaje", "Recepcion disponible"),
    }


@app.post("/disponibilidad")
def set_disponibilidad(
    payload: SetDisponibilidadRequest,
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)
    db.collection("config").document("recepcion").set(
        {
            "disponible": payload.disponible,
            "mensaje": payload.mensaje,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedAtIso": now_iso(),
        },
        merge=True,
    )
    return {"ok": True}


@app.post("/avisos")
@app.get("/avisos")
def list_avisos():
    docs = db.collection("avisos").stream()
    resultado = []

    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        resultado.append(data)
        
    return resultado
def create_aviso(payload: CreateAvisoRequest):
    if payload.tipo not in VALID_CREATE_TYPES:
        raise HTTPException(status_code=400, detail="Tipo de aviso no valido")

    if payload.origenCanal not in VALID_CHANNELS:
        raise HTTPException(status_code=400, detail="Canal no valido")

    disponibilidad = get_disponibilidad()
    if not disponibilidad["disponible"]:
        raise HTTPException(status_code=503, detail=disponibilidad["mensaje"])

    daily_link = payload.dailyLink or "https://ndhoteles.daily.co/ndhoteles?theme=dark"

    doc_ref = db.collection("avisos").document()
    doc_ref.set(
        {
            "hotel": payload.hotel.strip(),
            "punto": payload.punto.strip(),
            "tipo": payload.tipo,
            "dailyLink": daily_link,
            "estado": "nuevo",
            "origen": "backend_api",
            "origenCanal": payload.origenCanal,
            "nombreHuesped": payload.nombreHuesped.strip(),
            "telefonoContacto": payload.telefonoContacto.strip(),
            "anotacionAgente": payload.anotacionAgente.strip(),
            "creadoManual": payload.creadoManual,
            "retornarKiosko": False,
            "enHistorico": False,
            "creado": firestore.SERVER_TIMESTAMP,
            "creadoIso": now_iso(),
            "fechaAnotacion": now_iso(),
        }
    )

    return {"ok": True, "id": doc_ref.id}


@app.post("/avisos/{aviso_id}/estado")
def update_estado(
    aviso_id: str,
    payload: UpdateEstadoRequest,
    x_agent_token: Optional[str] = Header(default=None),
):
    require_agent(x_agent_token)
    current = get_aviso_data_or_404(aviso_id)
    current_estado = (current.get("estado") or "nuevo").lower()
    next_estado = payload.estado.lower()

    validate_transition(current_estado, next_estado)

    patch: Dict[str, Any] = {
        "estado": next_estado,
        "fechaAnotacion": now_iso(),
        "updatedAt": firestore.SERVER_TIMESTAMP,
        "updatedAtIso": now_iso(),
    }

    if payload.resultado is not None:
        patch["resultado"] = payload.resultado

    if payload.retornarKiosko is not None:
        patch["retornarKiosko"] = payload.retornarKiosko

    if next_estado in {"finalizada", "resuelta", "retorno_inicio"}:
        patch["fechaFin"] = now_iso()

    get_aviso_ref(aviso_id).update(patch)
    return {"ok": True, "id": aviso_id, "estado": next_estado}


@app.post("/avisos/{aviso_id}/contacto")
def update_contacto(
    aviso_id: str,
    payload: UpdateContactoRequest,
    x_agent_token: Optional[str] = Header(default=None),
):
    require_agent(x_agent_token)
    get_aviso_data_or_404(aviso_id)
    get_aviso_ref(aviso_id).update(
        {
            "nombreHuesped": payload.nombreHuesped.strip(),
            "telefonoContacto": payload.telefonoContacto.strip(),
            "fechaAnotacion": now_iso(),
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedAtIso": now_iso(),
        }
    )
    return {"ok": True, "id": aviso_id}


@app.post("/avisos/{aviso_id}/anotacion")
def update_anotacion(
    aviso_id: str,
    payload: UpdateAnotacionRequest,
    x_agent_token: Optional[str] = Header(default=None),
):
    require_agent(x_agent_token)
    get_aviso_data_or_404(aviso_id)
    get_aviso_ref(aviso_id).update(
        {
            "anotacionAgente": payload.anotacionAgente.strip(),
            "fechaAnotacion": now_iso(),
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "updatedAtIso": now_iso(),
        }
    )
    return {"ok": True, "id": aviso_id}


@app.delete("/avisos/{aviso_id}")
def delete_aviso(
    aviso_id: str,
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)
    get_aviso_data_or_404(aviso_id)
    get_aviso_ref(aviso_id).delete()
    return {"ok": True, "id": aviso_id}


@app.post("/admin/reset")
def reset_avisos(
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)
    docs = db.collection("avisos").stream()
    deleted = 0
    for snap in docs:
        snap.reference.delete()
        deleted += 1
    return {"ok": True, "deleted": deleted}
