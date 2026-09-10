"""SQLAlchemy ORM models for all database tables."""
from db.models.user import User
from db.models.preference import UserPreference
from db.models.project import Project
from db.models.session import Session
from db.models.message import Message
from db.models.part import Part
from db.models.internal_part import InternalPart
from db.models.permission import PermissionRule
from db.models.container import Container
from db.models.cloud_desktop import CloudDesktop
from db.models.desktop_activation import DesktopActivation
from db.models.todo import Todo
from db.models.prompt_history import PromptHistory
from db.models.file_asset import FileAsset
from db.models.audit_log import AuditLog
from db.models.cron import CronJob, CronRun
from db.models.video_job import VideoJob
from db.models.video_production import VideoApproval, VideoProduction, VideoSegment
from db.models.user_skill import UserSkill
from db.models.skill_install import SkillInstall
from db.models.catalog_override import CatalogOverride
from db.models.skill_catalog_package import SkillCatalogPackage
from db.models.memory import UserMemory
from db.models.image_gen_cache import ImageGenCache
from db.models.workspace import Workspace, WorkspaceMember, WorkspaceInvitation
from db.models.internal_task import InternalTaskState
from db.models.billing import CreditBalance, CreditLedger, UsageEvent, PaymentOrder, PaymentOrderRequest, BillingSubscription
from db.models.fleet import FleetAlert, FleetSnapshot, PoolPurchase
from db.models.desktop_event import DesktopEvent
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob
from db.models.notification import Notification
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.hot_trend import HotMediaLink, HotTrendSnapshot
import db.models.workspace_scope  # noqa: F401,E402

__all__ = [
    "User", "UserPreference", "Project", "Session", "Message", "Part", "InternalPart",
    "PermissionRule", "Container", "CloudDesktop", "DesktopActivation", "Todo", "PromptHistory", "FileAsset", "AuditLog",
    "CronJob", "CronRun", "VideoJob", "VideoProduction", "VideoSegment", "VideoApproval",
    "UserSkill", "SkillInstall", "CatalogOverride", "SkillCatalogPackage", "UserMemory", "ImageGenCache",
    "Workspace", "WorkspaceMember", "WorkspaceInvitation", "InternalTaskState",
    "CreditBalance", "CreditLedger", "UsageEvent", "PaymentOrder", "PaymentOrderRequest", "BillingSubscription",
    "FleetAlert", "FleetSnapshot", "PoolPurchase",
    "PlatformAccount", "PublishJob", "Notification", "QuestionCheckpoint", "SessionExecution",
    "HotTrendSnapshot", "HotMediaLink",
]
