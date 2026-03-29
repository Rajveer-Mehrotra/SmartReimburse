import uuid
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from fastapi import HTTPException
from app.models import Expense, ExpenseApproval, User
from app.schemas.expenses import ExpenseCreate, ApproveRejectRequest

CATEGORIES = ["Travel", "Meals", "Accommodation", "Equipment", "Software", "Training", "Medical", "Other"]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _enrich_expense(expense: Expense) -> Expense:
    """Attach employee_name and approver_name for response serialization."""
    expense.employee_name = expense.employee.name if expense.employee else None
    for step in expense.approval_steps:
        step.approver_name = step.approver.name if step.approver else None
    return expense


def _get_expense_or_404(db: Session, expense_id: uuid.UUID, company_id: uuid.UUID) -> Expense:
    expense = db.query(Expense).filter(
        Expense.id == expense_id,
        Expense.company_id == company_id
    ).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


# ─── Create ───────────────────────────────────────────────────────────────────

def create_expense(db: Session, employee_id: uuid.UUID, company_id: uuid.UUID, data: ExpenseCreate) -> Expense:
    expense = Expense(
        employee_id=employee_id,
        company_id=company_id,
        amount=data.original_amount,   # Will be converted on submit
        original_amount=data.original_amount,
        currency=data.currency,
        category=data.category,
        description=data.description,
        expense_date=data.expense_date,
        receipt_url=data.receipt_url,
        status="draft",
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return _enrich_expense(expense)


# ─── Submit ───────────────────────────────────────────────────────────────────

def submit_expense(db: Session, expense_id: uuid.UUID, employee_id: uuid.UUID, company_id: uuid.UUID) -> Expense:
    expense = db.query(Expense).filter(
        Expense.id == expense_id,
        Expense.employee_id == employee_id,
        Expense.company_id == company_id,
    ).first()
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    if expense.status != "draft":
        raise HTTPException(status_code=400, detail=f"Cannot submit expense in '{expense.status}' status")

    # Delete any old approval steps
    db.query(ExpenseApproval).filter(ExpenseApproval.expense_id == expense_id).delete(synchronize_session=False)
    expense.status = "pending"

    employee = db.query(User).filter(User.id == employee_id).first()
    if not employee or not employee.manager_id:
        raise HTTPException(status_code=400, detail="Employee must have a manager assigned before submitting")

    admin_user = db.query(User).filter(
        User.company_id == company_id,
        User.role == "admin",
    ).order_by(User.created_at.asc()).first()
    if not admin_user:
        raise HTTPException(status_code=400, detail="No admin found for this company")

    db.add_all([
        ExpenseApproval(
            expense_id=expense.id,
            approver_id=employee.manager_id,
            step_order=1,
            status="pending",
        ),
        ExpenseApproval(
            expense_id=expense.id,
            approver_id=admin_user.id,
            step_order=2,
            status="waiting",
        ),
    ])

    db.commit()
    db.refresh(expense)
    return _enrich_expense(expense)


# ─── Queries ─────────────────────────────────────────────────────────────────

def get_my_expenses(db: Session, employee_id: uuid.UUID, company_id: uuid.UUID):
    expenses = db.query(Expense).filter(
        Expense.employee_id == employee_id,
        Expense.company_id == company_id,
    ).order_by(Expense.created_at.desc()).all()
    return [_enrich_expense(e) for e in expenses]


def get_team_expenses(db: Session, manager_id: uuid.UUID, company_id: uuid.UUID):
    # Get employees whose manager is this user
    subordinate_ids = [
        u.id for u in db.query(User).filter(
            User.manager_id == manager_id,
            User.company_id == company_id,
        ).all()
    ]
    if not subordinate_ids:
        return []
    expenses = db.query(Expense).filter(
        Expense.employee_id.in_(subordinate_ids),
        Expense.company_id == company_id,
    ).order_by(Expense.created_at.desc()).all()
    return [_enrich_expense(e) for e in expenses]


def get_all_expenses(db: Session, company_id: uuid.UUID):
    expenses = db.query(Expense).filter(
        Expense.company_id == company_id,
    ).order_by(Expense.created_at.desc()).all()
    return [_enrich_expense(e) for e in expenses]


def get_pending_approvals(db: Session, approver_id: uuid.UUID, company_id: uuid.UUID):
    """Get expenses where this user has the active pending approval step."""
    expenses = db.query(Expense).join(
        ExpenseApproval, ExpenseApproval.expense_id == Expense.id
    ).filter(
        ExpenseApproval.approver_id == approver_id,
        ExpenseApproval.status == "pending",
        Expense.company_id == company_id,
        Expense.status == "pending",
    ).order_by(Expense.created_at.desc()).all()

    return [_enrich_expense(exp) for exp in expenses]


# ─── Approve / Reject ─────────────────────────────────────────────────────────

def approve_expense(db: Session, expense_id: uuid.UUID, approver_id: uuid.UUID, company_id: uuid.UUID, data: ApproveRejectRequest) -> Expense:
    expense = _get_expense_or_404(db, expense_id, company_id)
    if expense.status != "pending":
        raise HTTPException(status_code=400, detail="Expense is not pending approval")

    # Find this approver's step
    step = db.query(ExpenseApproval).filter(
        ExpenseApproval.expense_id == expense_id,
        ExpenseApproval.approver_id == approver_id,
        ExpenseApproval.status == "pending",
    ).first()
    if not step:
        raise HTTPException(status_code=403, detail="You are not an approver for this expense or already actioned")

    # Enforce sequential ordering
    pending_orders = sorted({s.step_order for s in expense.approval_steps if s.status == "pending"})
    if pending_orders and step.step_order != pending_orders[0]:
        raise HTTPException(status_code=400, detail="Waiting for earlier approvals")

    step.status = "approved"
    step.comment = data.comment
    step.approved_at = datetime.now(timezone.utc)
    db.commit()

    # Activate next step if waiting, otherwise approve expense
    next_step = db.query(ExpenseApproval).filter(
        ExpenseApproval.expense_id == expense_id,
        ExpenseApproval.status == "waiting",
    ).order_by(ExpenseApproval.step_order.asc()).first()
    if next_step:
        next_step.status = "pending"
    else:
        pending_count = db.query(ExpenseApproval).filter(
            ExpenseApproval.expense_id == expense_id,
            ExpenseApproval.status == "pending",
        ).count()
        if pending_count == 0:
            expense.status = "approved"

    db.commit()
    db.refresh(expense)
    return _enrich_expense(expense)


def reject_expense(db: Session, expense_id: uuid.UUID, approver_id: uuid.UUID, company_id: uuid.UUID, data: ApproveRejectRequest) -> Expense:
    expense = _get_expense_or_404(db, expense_id, company_id)
    if expense.status != "pending":
        raise HTTPException(status_code=400, detail="Expense is not pending approval")

    step = db.query(ExpenseApproval).filter(
        ExpenseApproval.expense_id == expense_id,
        ExpenseApproval.approver_id == approver_id,
        ExpenseApproval.status == "pending",
    ).first()
    if not step:
        raise HTTPException(status_code=403, detail="You are not an approver for this expense or already actioned")

    pending_orders = sorted({s.step_order for s in expense.approval_steps if s.status == "pending"})
    if pending_orders and step.step_order != pending_orders[0]:
        raise HTTPException(status_code=400, detail="Waiting for earlier approvals")

    step.status = "rejected"
    step.comment = data.comment
    step.approved_at = datetime.now(timezone.utc)
    expense.status = "rejected"
    db.commit()
    db.refresh(expense)
    return _enrich_expense(expense)
