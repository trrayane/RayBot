import openpyxl
from dataclasses import dataclass


@dataclass
class Student:
    numero: str
    palier: str
    specialite: str
    section: str
    matricule: str
    nom: str
    prenom: str
    etat: str
    groupe_td: str
    groupe_tp: str

    @property
    def full_name(self) -> str:
        return f"{self.nom} {self.prenom}".strip()

    @property
    def is_sii(self) -> bool:
        return (self.specialite or "").strip().upper() in ("SII", "MIV", "MIV STUDENT")

    @property
    def is_admis(self) -> bool:
        return (self.etat or "").strip().upper() == "ADM"


def load_students(path: str) -> dict[str, Student]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))

    header_row = None
    for i, row in enumerate(rows):
        if len(row) > 4 and row[4] == "Matricule":
            header_row = i
            break
    if header_row is None:
        raise ValueError("Could not find header row with 'Matricule' in the xlsx file.")

    students: dict[str, Student] = {}
    skipped = 0
    for row in rows[header_row + 1:]:
        matricule = row[4]
        if matricule is None or str(matricule).strip() == "":
            continue
        matricule = str(matricule).strip().upper()
        numero = row[0]
        palier = str(row[1]) if row[1] is not None else ""
        specialite = str(row[2]) if row[2] is not None else ""
        section = str(row[3]) if row[3] is not None else ""
        nom = str(row[5]) if row[5] is not None else ""
        prenom = str(row[6]) if row[6] is not None else ""
        etat = str(row[7]) if row[7] is not None else ""
        groupe_td = _num(row[8])
        groupe_tp = _num(row[9])
        students[matricule] = Student(
            numero=str(int(numero)) if isinstance(numero, (int, float)) else str(numero),
            palier=palier,
            specialite=specialite,
            section=section,
            matricule=matricule,
            nom=nom,
            prenom=prenom,
            etat=etat,
            groupe_td=groupe_td,
            groupe_tp=groupe_tp,
        )
    return students, skipped


def _num(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if value is None:
        return ""
    return str(value).strip()