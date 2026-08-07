'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import {
  ArrowLeft,
  ShieldCheck,
  Calendar,
  DollarSign,
  Hash,
  Store,
  Package,
  Send,
  Bot,
  User,
  Trash2,
  MessageSquare,
  CalendarPlus,
  Download,
  BadgeCheck,
  AlertTriangle,
  Languages,
  RefreshCw,
  ExternalLink,
  Copy,
  MapPin,
  Phone,
  Globe,
} from 'lucide-react'
import { apiClient } from '@/lib/api-client'
import { useGsapReveal } from '@/lib/gsap-helpers'
import { getCurrentLocation } from '@/lib/location'
import { useAuthStore } from '@/lib/store/auth-store'
import type { Document, User as AuthUser } from '@/lib/types'
import { ProductVisual } from '@/components/product-visual'

interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  supportPoints?: string[]
  missingInformation?: string[]
  confidenceNote?: string
  sources?: Array<{
    docId: string
    chunk: string
    score: number
  }>
  serviceCenters?: ServiceCenterItem[]
}

interface CalendarLinksResponse {
  docId: string
  googleCalendarUrl: string
  icsDownloadUrl: string
}

interface ClaimPacketResponse {
  docId: string
  generatedAt: string
  facts: Record<string, unknown>
  timeline: string[]
  issueSummaryTemplate: string
  emailTemplate: string
  attachmentChecklist: string[]
}

interface ClaimAssistantResponse {
  docId: string
  deadlineBand?: string | null
  nextBestActions: string[]
  recommendedChannels: string[]
}

interface SourceUrlResponse {
  docId: string
  url: string
  expiresInSeconds: number
}

interface ServiceCenterItem {
  name: string
  address: string
  distance_km?: number | null
  source?: string | null
  phone?: string | null
  website?: string | null
  map_url?: string | null
  city?: string | null
  pickup_available?: boolean | null
  estimated_tat_days?: number | null
}

interface ServiceCentersResponse {
  docId: string
  company?: string | null
  locationHint?: string | null
  radiusKm?: number
  guidance: string
  centers: ServiceCenterItem[]
  count: number
}

interface BharatAIInsight {
  sourceLanguageCode?: string
  targetLanguageCode?: string
  normalizedText?: string
  localizedSummary?: string
  consumerSummary?: string
  gstFindings?: string[]
  fraudSignals?: string[]
  claimSteps?: string[]
  merchantNotes?: string[]
  paymentReferences?: string[]
  modelUsed?: string | null
  speechAudioBase64?: string | null
  speechContentType?: string | null
}

interface BharatAIAskResponse {
  sourceLanguageCode?: string
  targetLanguageCode?: string
  normalizedQuestion?: string
  localizedQuestion?: string
  answer?: string
  supportPoints?: string[]
  missingInformation?: string[]
  confidenceNote?: string
  modelUsed?: string | null
  error?: string
  detail?: string
}

interface BharatAITranslateBatchResponse {
  translations?: string[]
  error?: string
  detail?: string
}

interface AssignmentAckResponse {
  assignmentId: string
  documentId: string
  merchantUserId: string
  consumerUserId: string
  status: string
  acceptedAt?: string | null
  escalatedAt?: string | null
  notes?: string | null
}

interface TranslationEntry {
  key: string
  text: string
}

const DAY_MS = 24 * 60 * 60 * 1000
const HOUR_MS = 60 * 60 * 1000
const MINUTE_MS = 60 * 1000
const ACTION_NOW_DAYS = 7
const COMING_UP_DAYS = 30

type DeadlineLevel = 'action_now' | 'coming_up' | 'on_track' | 'expired'

interface DeadlineMeta {
  level: DeadlineLevel
  label: string
  hint: string
  countdown: string
  badgeClass: string
  textClass: string
}

function formatCountdown(msLeft: number): string {
  const absolute = Math.abs(msLeft)
  const days = Math.floor(absolute / DAY_MS)
  const hours = Math.floor((absolute % DAY_MS) / HOUR_MS)
  const minutes = Math.floor((absolute % HOUR_MS) / MINUTE_MS)

  if (msLeft <= 0) {
    if (days > 0) return `${days}d ago`
    if (hours > 0) return `${hours}h ago`
    return `${Math.max(minutes, 1)}m ago`
  }

  if (days > 0) return `${days}d ${hours}h ${minutes}m`
  if (hours > 0) return `${hours}h ${minutes}m`
  return `${Math.max(minutes, 1)}m`
}

function getDeadlineMeta(warrantyEnd: string | undefined, nowMs: number): DeadlineMeta | null {
  if (!warrantyEnd) return null
  const endMs = Date.parse(warrantyEnd)
  if (!Number.isFinite(endMs)) return null

  const msLeft = endMs - nowMs
  const daysLeft = Math.ceil(msLeft / DAY_MS)
  const countdown = formatCountdown(msLeft)

  if (daysLeft <= 0) {
    return { level: 'expired', label: 'Expired', hint: 'Warranty period ended', countdown, badgeClass: 'bg-slate-100 text-slate-500', textClass: 'text-slate-500' }
  }
  if (daysLeft <= ACTION_NOW_DAYS) {
    return { level: 'action_now', label: 'Action required', hint: 'Act now', countdown, badgeClass: 'bg-red-100 text-red-600', textClass: 'text-red-600' }
  }
  if (daysLeft <= COMING_UP_DAYS) {
    return { level: 'coming_up', label: 'Coming up', hint: 'Plan this week', countdown, badgeClass: 'bg-amber-100 text-amber-700', textClass: 'text-amber-600' }
  }
  return { level: 'on_track', label: 'On track', hint: 'No rush yet', countdown, badgeClass: 'bg-emerald-100 text-emerald-600', textClass: 'text-emerald-600' }
}

function complianceTone(status?: string) {
  if (status === 'pass') return { alertClass: 'bg-emerald-50 border-emerald-200 text-emerald-800', label: 'Compliant' }
  if (status === 'risk') return { alertClass: 'bg-red-50 border-red-200 text-red-800', label: 'Attention Needed' }
  return { alertClass: 'bg-amber-50 border-amber-200 text-amber-800', label: 'Review Suggested' }
}

const BHARAT_LANGUAGE_OPTIONS = [
  { code: 'en', label: 'English' },
  { code: 'hi', label: 'Hindi' },
  { code: 'ta', label: 'Tamil' },
  { code: 'te', label: 'Telugu' },
  { code: 'kn', label: 'Kannada' },
  { code: 'mr', label: 'Marathi' },
  { code: 'bn', label: 'Bengali' },
] as const

function formatCurrencyValue(value: number | null | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null
  return new Intl.NumberFormat('en-IN', {
    style: 'currency',
    currency: 'INR',
    maximumFractionDigits: 2,
  }).format(value)
}

function formatDisplayDate(value: string | undefined): string | null {
  if (!value) return null
  const parsed = Date.parse(value)
  if (!Number.isFinite(parsed)) return value
  return new Intl.DateTimeFormat('en-IN', {
    day: '2-digit',
    month: 'short',
    year: 'numeric',
  }).format(parsed)
}

function getDocumentInvoiceAmount(document: Document | null | undefined): number | null {
  if (!document) return null
  if (document.totalAmount != null && Number.isFinite(document.totalAmount)) {
    return document.totalAmount
  }
  const fallback = document.items?.[0]?.purchasePrice
  return fallback != null && Number.isFinite(fallback) ? fallback : null
}

function getDocumentInvoiceNumber(document: Document | null | undefined): string {
  return String(document?.items?.[0]?.invoiceNo || '').trim()
}

function getDocumentPurchaseDate(document: Document | null | undefined): string {
  return String(document?.items?.[0]?.purchaseDate || '').trim()
}

function formatInsightSummary(
  summary: string | undefined,
  fallback: {
    productName?: string
    sellerName?: string
    purchaseDate?: string
    amount?: number | null
  }
): string {
  const cleaned = String(summary || '')
    .replace(/\s*\|\s*/g, '. ')
    .replace(/\s{2,}/g, ' ')
    .trim()
  if (cleaned) return cleaned

  const bits: string[] = []
  if (fallback.productName) bits.push(`This looks like an invoice for ${fallback.productName}.`)
  if (fallback.sellerName) bits.push(`Seller: ${fallback.sellerName}.`)
  if (fallback.purchaseDate) bits.push(`Purchase date: ${fallback.purchaseDate}.`)
  if (fallback.amount != null) {
    const formattedAmount = formatCurrencyValue(fallback.amount)
    if (formattedAmount) bits.push(`Amount: ${formattedAmount}.`)
  }
  return bits.join(' ') || 'AI guidance will appear here once the invoice is analyzed.'
}

function formatModelLabel(model: string | null | undefined): string {
  const raw = String(model || '').trim().toLowerCase()
  if (!raw) return 'AWS Bedrock'
  if (raw.includes('nova-2-lite')) return 'Amazon Nova 2 Lite'
  if (raw.includes('nova-lite')) return 'Amazon Nova Lite'
  if (raw.includes('nova-pro')) return 'Amazon Nova Pro'
  return String(model)
}

function formatSupportSource(source: string | null | undefined): string {
  const raw = String(source || '').trim().toLowerCase()
  if (!raw) return 'Support'
  if (raw === 'google_maps') return 'Google Maps'
  if (raw === 'openstreetmap_overpass') return 'OpenStreetMap'
  if (raw === 'openstreetmap_nominatim') return 'OpenStreetMap'
  if (raw === 'brand_directory') return 'Brand Directory'
  if (raw === 'official_support') return 'Official Support'
  return raw.replace(/_/g, ' ')
}

function formatSupportSummary(payload: ServiceCentersResponse | null): string {
  if (!payload) {
    return 'Search when you need repair or warranty service help.'
  }
  if (!payload.centers.length) {
    return payload.guidance || 'No nearby service centers were returned yet.'
  }

  const company = String(payload.company || 'this brand').trim()
  const location = String(payload.locationHint || '').trim()
  const placeText = location ? ` near ${location}` : ' near your location'
  const countText = payload.count === 1 ? '1 support location' : `${payload.count} support locations`
  return `Found ${countText} for ${company}${placeText}.`
}

function buildTranslationMap(entries: TranslationEntry[], translations: string[]): Record<string, string> {
  const output: Record<string, string> = {}
  entries.forEach((entry, index) => {
    const translated = String(translations[index] || '').trim()
    if (!entry.key || !translated) return
    output[entry.key] = translated
  })
  return output
}

function buildInvoiceAiMetadata(
  document: Document | null,
  serviceCenters: ServiceCentersResponse | null = null
): Record<string, unknown> {
  const item = document?.items?.[0]
  const invoiceNumber = getDocumentInvoiceNumber(document)
  const invoiceAmount = getDocumentInvoiceAmount(document)
  const purchaseDate = getDocumentPurchaseDate(document)
  const complianceAlerts =
    document?.compliance?.alerts?.map((alert) => String(alert.message || '').trim()).filter(Boolean) || []

  return {
    bill_id: invoiceNumber,
    vendor: document?.sellerName || '',
    product_name: item?.productName || document?.title || '',
    total_amount: invoiceAmount,
    gst_amount: item?.gstAmount ?? document?.gstAmount ?? null,
    taxable_amount: document?.taxableAmount ?? null,
    date: purchaseDate,
    category: document?.category || '',
    title: document?.title || '',
    model: item?.model || '',
    serial_number: item?.serialNumber || '',
    warranty_months: item?.warrantyMonths ?? null,
    warranty_start: item?.warrantyStart || '',
    warranty_end: item?.warrantyEnd || '',
    compliance_status: document?.compliance?.status || '',
    compliance_alerts: complianceAlerts.slice(0, 4),
    service_center_company: serviceCenters?.company || '',
    service_center_guidance: serviceCenters?.guidance || '',
  }
}

function formatSupportMeta(center: ServiceCenterItem): string[] {
  const meta: string[] = []
  if (center.distance_km != null) {
    meta.push(`${center.distance_km.toFixed(1)} km away`)
  }
  if (center.estimated_tat_days != null) {
    meta.push(`TAT ${center.estimated_tat_days} day${center.estimated_tat_days === 1 ? '' : 's'}`)
  }
  if (center.pickup_available) {
    meta.push('Pickup available')
  }
  if (center.city) {
    meta.push(center.city)
  }
  return meta
}

function getInsightState({
  loading,
  error,
  reviewRequired,
  fraudSignalCount,
  claimStepCount,
}: {
  loading: boolean
  error: string | null
  reviewRequired: boolean
  fraudSignalCount: number
  claimStepCount: number
}) {
  if (loading) {
    return {
      label: 'Refreshing',
      note: 'Re-reading OCR and rebuilding guidance.',
      className: 'border-sky-400/30 bg-sky-400/10 text-sky-100',
    }
  }
  if (error) {
    return {
      label: 'Needs retry',
      note: 'The AI summary could not be refreshed.',
      className: 'border-red-400/30 bg-red-400/10 text-red-100',
    }
  }
  if (reviewRequired || fraudSignalCount > 0) {
    return {
      label: 'Review before claim',
      note: 'Some extracted details should be checked manually.',
      className: 'border-amber-300/30 bg-amber-300/10 text-amber-50',
    }
  }
  if (claimStepCount > 0) {
    return {
      label: 'Action ready',
      note: 'The assistant prepared next steps from this invoice.',
      className: 'border-emerald-300/30 bg-emerald-300/10 text-emerald-50',
    }
  }
  return {
    label: 'Summary ready',
    note: 'Key invoice facts were captured successfully.',
    className: 'border-white/15 bg-white/5 text-slate-100',
  }
}

function uniqueItems(values: Array<string | null | undefined>): string[] {
  const seen = new Set<string>()
  const result: string[] = []
  values.forEach((value) => {
    const cleaned = String(value || '').trim()
    if (!cleaned) return
    if (seen.has(cleaned)) return
    seen.add(cleaned)
    result.push(cleaned)
  })
  return result
}

function buildDocumentScopeParams(user: AuthUser | null | undefined): Record<string, string> | undefined {
  if (!user?.userId) return undefined
  if (user.userType === 'merchant') {
    return { merchantUserId: user.userId }
  }
  return { userId: user.userId }
}

export function DocumentDetailScreen({ docId }: { docId: string }) {
  const router = useRouter()
  const { user, token } = useAuthStore()
  const rootRef = useRef<HTMLDivElement>(null)
  const [document, setDocument] = useState<Document | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [isDeleting, setIsDeleting] = useState(false)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [chatInput, setChatInput] = useState('')
  const [isChatLoading, setIsChatLoading] = useState(false)
  const [showChat, setShowChat] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())
  const [calendarLinks, setCalendarLinks] = useState<CalendarLinksResponse | null>(null)
  const [calendarLoading, setCalendarLoading] = useState(false)
  const [claimPacket, setClaimPacket] = useState<ClaimPacketResponse | null>(null)
  const [claimPacketLoading, setClaimPacketLoading] = useState(false)
  const [claimAssistant, setClaimAssistant] = useState<ClaimAssistantResponse | null>(null)
  const [claimAssistantLoading, setClaimAssistantLoading] = useState(false)
  const [sourceUrlLoading, setSourceUrlLoading] = useState(false)
  const [serviceCenters, setServiceCenters] = useState<ServiceCentersResponse | null>(null)
  const [serviceCentersLoading, setServiceCentersLoading] = useState(false)
  const [serviceCentersError, setServiceCentersError] = useState<string | null>(null)
  const [serviceCentersStatus, setServiceCentersStatus] = useState<string | null>(null)
  const [bharatInsight, setBharatInsight] = useState<BharatAIInsight | null>(null)
  const [bharatLoading, setBharatLoading] = useState(false)
  const [bharatLanguage, setBharatLanguage] = useState('en')
  const [bharatError, setBharatError] = useState<string | null>(null)
  const [bharatAudioSrc, setBharatAudioSrc] = useState<string | null>(null)
  const [bharatSpeechLoading, setBharatSpeechLoading] = useState(false)
  const [pageTranslations, setPageTranslations] = useState<Record<string, string>>({})
  const [pageTranslationLoading, setPageTranslationLoading] = useState(false)
  const [assignmentActionLoading, setAssignmentActionLoading] = useState<'accepted' | 'escalated' | null>(null)
  const [assignmentActionError, setAssignmentActionError] = useState<string | null>(null)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const documentScopeParams = useMemo(() => buildDocumentScopeParams(user), [user])
  const defaultHomeRoute = user?.userType === 'merchant' ? '/merchant-dashboard' : '/locker'

  const translateUi = useCallback(
    (key: string, fallback: string) => {
      if (bharatLanguage === 'en') return fallback
      const translated = String(pageTranslations[key] || '').trim()
      return translated || fallback
    },
    [bharatLanguage, pageTranslations]
  )

  const runBharatEnrich = useCallback(
    async ({ includeSpeech = false, languageCode }: { includeSpeech?: boolean; languageCode?: string } = {}) => {
      if (!document?.rawText?.trim()) {
        setBharatInsight(null)
        setBharatError(null)
        setBharatAudioSrc(null)
        return false
      }

      const targetLanguageCode = (languageCode || bharatLanguage || 'en').toLowerCase()
      try {
        if (includeSpeech) {
          setBharatSpeechLoading(true)
        } else {
          setBharatLoading(true)
          setBharatAudioSrc(null)
        }
        setBharatError(null)

        const headers: HeadersInit = { 'Content-Type': 'application/json' }
        if (token) {
          headers.Authorization = `Bearer ${token}`
        }

        const response = await fetch('/api/ai/bharat/enrich', {
          method: 'POST',
          headers,
          body: JSON.stringify({
            ocrText: document.rawText,
            metadata: buildInvoiceAiMetadata(document, serviceCenters),
            targetLanguageCode,
            includeSpeech,
          }),
        })

        const payload = (await response.json().catch(() => null)) as (BharatAIInsight & { error?: string; detail?: string }) | null
        if (!response.ok || !payload) {
          const message =
            (typeof payload?.error === 'string' && payload.error.trim()) ||
            (typeof payload?.detail === 'string' && payload.detail.trim()) ||
            'Unable to load Bharat AI insight.'
          setBharatError(message)
          return false
        }

        setBharatInsight(payload)
        if (payload.speechAudioBase64 && payload.speechContentType) {
          setBharatAudioSrc(`data:${payload.speechContentType};base64,${payload.speechAudioBase64}`)
        } else if (includeSpeech) {
          setBharatError('Voice summary is not available for this language/configuration yet.')
        }
        return true
      } catch (error) {
        const message =
          error instanceof Error && error.message.trim()
            ? error.message
            : 'Unable to load Bharat AI insight.'
        setBharatError(message)
        return false
      } finally {
        if (includeSpeech) {
          setBharatSpeechLoading(false)
        } else {
          setBharatLoading(false)
        }
      }
    },
    [bharatLanguage, document, serviceCenters, token]
  )

  const loadDocument = useCallback(async () => {
    try {
      setIsLoading(true)
      const payload = await apiClient.get<Document>(`/documents/${docId}`, {
        params: documentScopeParams,
      })
      setDocument(payload)
    } catch (error) {
      console.error('Failed to load document:', error)
      setDocument(null)
    } finally {
      setIsLoading(false)
    }
  }, [docId, documentScopeParams])

  const handleAssignmentAction = useCallback(
    async (status: 'accepted' | 'escalated') => {
      if (!document || !user?.userId) return
      setAssignmentActionLoading(status)
      setAssignmentActionError(null)
      try {
        await apiClient.post<AssignmentAckResponse>(`/documents/${docId}/assignment/ack`, {
          consumer_user_id: user.userId,
          status,
          notes:
            status === 'accepted'
              ? 'Consumer confirmed the merchant assignment from the document page.'
              : 'Consumer escalated the merchant assignment from the document page.',
        })
        await loadDocument()
      } catch (error) {
        setAssignmentActionError(
          error instanceof Error && error.message.trim()
            ? error.message
            : 'Unable to update assignment status.'
        )
      } finally {
        setAssignmentActionLoading(null)
      }
    },
    [docId, document, loadDocument, user?.userId]
  )

  useEffect(() => { loadDocument() }, [loadDocument])
  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages])
  useEffect(() => {
    const timerId = window.setInterval(() => setNowMs(Date.now()), 60000)
    return () => window.clearInterval(timerId)
  }, [])
  useEffect(() => {
    if (!document?.rawText?.trim()) {
      setBharatInsight(null)
      setBharatError(null)
      setBharatAudioSrc(null)
      return
    }
    void runBharatEnrich({ includeSpeech: false, languageCode: bharatLanguage })
  }, [bharatLanguage, document?.docId, document?.rawText, runBharatEnrich])
  useEffect(() => {
    let cancelled = false

    const translatePage = async () => {
      if (bharatLanguage === 'en' || !document) {
        setPageTranslations({})
        setPageTranslationLoading(false)
        return
      }

      const currentItem = document.items?.[0]
      const currentInvoiceAmount = getDocumentInvoiceAmount(document)
      const currentDeadline = getDeadlineMeta(currentItem?.warrantyEnd, nowMs)
      const currentClaimReadinessSummary = document.claimReadiness?.summary || ''
      const currentComplianceAlerts = (document.compliance?.alerts || [])
        .slice(0, 2)
        .map((alert, index) => ({
          key: `compliance.alert.${index}`,
          text: String(alert.message || ''),
        }))
      const currentNextActionItems = uniqueItems([
        ...(claimAssistant?.nextBestActions || []),
        ...(bharatInsight?.claimSteps || []),
        ...(claimPacket?.timeline || []),
      ]).slice(0, 5)
      const currentVerificationItems = uniqueItems([
        ...(document.compliance?.alerts?.map((alert) => alert.message) || []),
        ...(bharatInsight?.fraudSignals || []),
        ...(document.reviewRequired
          ? [`Double-check extracted fields: ${(document.lowConfidenceFields || []).join(', ') || 'invoice details'}.`]
          : []),
      ]).slice(0, 5)
      const currentQuickQuestions = uniqueItems([
        'Summarize what this invoice covers and how much warranty time is left.',
        'What proof should I keep ready before I contact support?',
        currentItem?.invoiceNo ? `Check whether invoice ${currentItem.invoiceNo} has any claim or compliance risk.` : '',
        'What details are still missing or unclear in this invoice?',
      ]).slice(0, 4)
      const entries: TranslationEntry[] = [
        { key: 'nav.back', text: 'Back' },
        { key: 'nav.delete', text: 'Delete' },
        { key: 'nav.deleting', text: 'Deleting...' },
        { key: 'status.saved', text: 'Saved' },
        { key: 'review.title', text: 'Verification recommended' },
        {
          key: 'review.detail',
          text: `Low confidence in: ${(document.lowConfidenceFields || []).join(', ') || 'invoice fields'}`,
        },
        { key: 'claimReadiness.title', text: 'Claim Readiness' },
        { key: 'claimReadiness.summary', text: currentClaimReadinessSummary },
        { key: 'compliance.gstEinvoice', text: 'GST + e-Invoice' },
        { key: 'compliance.gstin', text: 'GSTIN' },
        { key: 'compliance.alerts', text: 'Alerts' },
        { key: 'compliance.notDetected', text: 'Not detected' },
        { key: 'compliance.validated', text: '(validated)' },
        { key: 'compliance.verify', text: '(verify manually)' },
        { key: 'compliance.label', text: complianceTone(document.compliance?.status).label },
        { key: 'assignment.label', text: 'Merchant Assignment' },
        { key: 'assignment.title', text: 'Assigned by merchant' },
        { key: 'assignment.description', text: 'Confirm that this invoice belongs in your locker or escalate it back to the merchant for correction.' },
        { key: 'assignment.assignedBy', text: 'Assigned by' },
        { key: 'assignment.status', text: 'Status' },
        { key: 'assignment.accept', text: 'Accept Assignment' },
        { key: 'assignment.escalate', text: 'Escalate to Merchant' },
        { key: 'assignment.accepting', text: 'Accepting...' },
        { key: 'assignment.escalating', text: 'Escalating...' },
        { key: 'assignment.accepted', text: 'Accepted' },
        { key: 'assignment.escalated', text: 'Escalated' },
        { key: 'assignment.pending', text: 'Awaiting your confirmation' },
        { key: 'assignment.synced', text: 'Merchant and consumer tracking stay synchronized after you respond here.' },
        { key: 'assignment.acceptedNote', text: 'You already confirmed this invoice. The merchant audit trail now shows it as accepted.' },
        { key: 'assignment.escalatedNote', text: 'You escalated this assignment. The merchant workspace will see the escalation status.' },
        { key: 'assignment.error', text: assignmentActionError || '' },
        { key: 'bharat.heading', text: 'What this invoice means' },
        { key: 'bharat.loading', text: 'Reviewing OCR text and building claim guidance.' },
        { key: 'bharat.personalize', text: 'Personalize output' },
        { key: 'bharat.refresh', text: 'Refresh Insight' },
        { key: 'bharat.context', text: 'AI context' },
        { key: 'bharat.detected', text: 'Detected language' },
        { key: 'bharat.output', text: 'Output language' },
        { key: 'bharat.model', text: 'Base model' },
        { key: 'bharat.gst.title', text: 'GST And Invoice Checks' },
        { key: 'bharat.gst.desc', text: 'Signals extracted from invoice tax details.' },
        { key: 'bharat.gst.empty', text: 'No GST or invoice checks were generated.' },
        { key: 'bharat.merchant.title', text: 'Merchant Context' },
        { key: 'bharat.merchant.desc', text: 'Seller and invoice context worth keeping.' },
        { key: 'bharat.merchant.empty', text: 'No merchant notes were generated.' },
        { key: 'bharat.payment.title', text: 'Payment Proof' },
        { key: 'bharat.payment.desc', text: 'References detected from OCR text.' },
        { key: 'bharat.payment.empty', text: 'No payment references were detected. Keep UPI, SMS, or bank proof separately.' },
        { key: 'bharat.audio.title', text: 'Audio Summary' },
        { key: 'bharat.audio.ready', text: 'Play a spoken summary of this invoice in the selected language.' },
        { key: 'bharat.audio.empty', text: 'Generate a voice summary if you want a quick listenable explanation.' },
        { key: 'actions.openInvoice', text: 'Open Source Invoice' },
        { key: 'actions.openingInvoice', text: 'Opening Invoice...' },
        { key: 'actions.calendar', text: 'Add To Calendar' },
        { key: 'actions.exportReminder', text: 'Export Reminder' },
        { key: 'actions.claimPacket', text: 'Prepare Claim Details' },
        { key: 'claimAssistant.label', text: 'Claim Assistant' },
        { key: 'claimAssistant.title', text: 'Action Plan' },
        { key: 'claimAssistant.loading', text: 'Loading claim playbook...' },
        { key: 'claimAssistant.next.title', text: 'Do Next' },
        { key: 'claimAssistant.next.desc', text: 'Immediate actions from the claim assistant and invoice AI.' },
        { key: 'claimAssistant.next.empty', text: 'No action items were generated yet.' },
        { key: 'claimAssistant.verify.title', text: 'Verify Before Claim' },
        { key: 'claimAssistant.verify.desc', text: 'Check these points before you submit anything.' },
        { key: 'claimAssistant.verify.empty', text: 'No blocking issues were flagged.' },
        { key: 'support.label', text: 'Service Help' },
        { key: 'support.title', text: 'Nearby Support' },
        { key: 'support.find', text: 'Find Centers' },
        { key: 'support.searching', text: 'Searching...' },
        { key: 'support.description', text: 'Find brand-authorized service locations using your current device location.' },
        { key: 'support.summary', text: formatSupportSummary(serviceCenters) },
        { key: 'support.guidance', text: serviceCenters?.guidance || '' },
        { key: 'support.empty', text: 'No nearby service centers were returned yet.' },
        { key: 'support.idle', text: 'Search when you need repair or warranty service help.' },
        { key: 'support.call', text: 'Call' },
        { key: 'support.website', text: 'Website' },
        { key: 'support.openMap', text: 'Open Map' },
        { key: 'claimPacket.label', text: 'Claim Details' },
        { key: 'claimPacket.title', text: 'Prepared For You' },
        { key: 'claimPacket.generatedAt', text: 'Generated at' },
        { key: 'claimPacket.copySummary', text: 'Copy Summary' },
        { key: 'claimPacket.copyEmail', text: 'Copy Email' },
        { key: 'claimPacket.step1.title', text: 'Review Summary' },
        { key: 'claimPacket.step1.desc', text: 'Start with the prepared issue summary before contacting support.' },
        { key: 'claimPacket.step2.title', text: 'Collect Evidence' },
        { key: 'claimPacket.step2.desc', text: claimPacket?.attachmentChecklist.slice(0, 2).join(' | ') || 'Gather bill, serial number, and issue proof.' },
        { key: 'claimPacket.step3.title', text: 'Find Support' },
        { key: 'claimPacket.step3.desc', text: 'Use nearby support centers before you escalate the claim.' },
        { key: 'claimPacket.step4.title', text: 'Send Claim' },
        { key: 'claimPacket.step4.desc', text: 'Use the drafted email and reminder tools to submit on time.' },
        { key: 'claimPacket.checklist.title', text: 'Checklist' },
        { key: 'claimPacket.checklist.desc', text: 'Documents and evidence to keep ready.' },
        { key: 'claimPacket.checklist.empty', text: 'No checklist items were generated.' },
        { key: 'claimPacket.timeline.title', text: 'Timeline' },
        { key: 'claimPacket.timeline.desc', text: 'Suggested order for the claim process.' },
        { key: 'claimPacket.timeline.empty', text: 'No timeline steps were generated.' },
        { key: 'claimPacket.facts', text: 'Claim Facts' },
        { key: 'claimPacket.issueSummary.title', text: 'Issue Summary' },
        { key: 'claimPacket.issueSummary.desc', text: 'Use this when submitting the problem to support.' },
        { key: 'claimPacket.email.title', text: 'Email Draft' },
        { key: 'claimPacket.email.desc', text: 'Use this as your warranty claim email.' },
        { key: 'sections.product', text: 'Product Information' },
        { key: 'sections.purchase', text: 'Purchase Information' },
        { key: 'sections.warranty', text: 'Warranty Information' },
        { key: 'label.productName', text: 'Product Name' },
        { key: 'label.brandModel', text: 'Brand/Model' },
        { key: 'label.category', text: 'Category' },
        { key: 'label.serialNumber', text: 'Serial Number' },
        { key: 'label.invoiceNo', text: 'Invoice No' },
        { key: 'label.storeSeller', text: 'Store / Seller' },
        { key: 'label.purchaseDate', text: 'Purchase Date' },
        { key: 'label.amount', text: 'Amount' },
        { key: 'label.warrantyPeriod', text: 'Warranty Period' },
        { key: 'label.warrantyStart', text: 'Warranty Start' },
        { key: 'label.warrantyEnd', text: 'Warranty End' },
        { key: 'label.deadlineStatus', text: 'Deadline Status' },
        { key: 'label.timeRemaining', text: 'Time Remaining' },
        { key: 'value.notFound', text: 'Not found' },
        { key: 'value.unknown', text: 'Unknown' },
        { key: 'value.na', text: 'N/A' },
        { key: 'value.others', text: 'Others' },
        { key: 'chat.show', text: 'Ask AI About This Warranty' },
        { key: 'chat.hide', text: 'Hide AI Chat' },
        { key: 'chat.title', text: 'Warranty Assistant' },
        { key: 'chat.subtitle', text: 'Grounded on your document and powered by SafeBill AI' },
        { key: 'chat.quick', text: 'Quick Questions' },
        { key: 'chat.placeholder', text: 'Ask about this warranty...' },
        { key: 'chat.grounding', text: 'Grounding' },
        { key: 'chat.supportPoints', text: 'Grounded Points' },
        { key: 'chat.missing', text: 'Still Not Visible On Invoice' },
        { key: 'chat.note', text: 'AI note' },
        { key: 'chat.greeting', text: `Hi! I can answer questions about your ${(currentItem?.productName || 'warranty').split('|')[0].replace(/[\*\s\:\-\[\]]+$/, '').trim()}.` },
        { key: 'chat.reference', text: 'reference' },
        { key: 'deadline.saved', text: 'Saved' },
        { key: 'deadline.left', text: 'left' },
        { key: 'deadline.ended', text: 'Ended' },
        { key: `deadline.label.${currentDeadline?.level || 'saved'}`, text: currentDeadline?.label || 'Saved' },
        { key: `deadline.hint.${currentDeadline?.level || 'saved'}`, text: currentDeadline?.hint || '' },
        { key: 'insight.state.label', text: bharatLoading ? 'Refreshing' : bharatError ? 'Needs retry' : document.reviewRequired || (bharatInsight?.fraudSignals?.length || 0) > 0 ? 'Review before claim' : (bharatInsight?.claimSteps?.length || 0) > 0 ? 'Action ready' : 'Summary ready' },
        { key: 'insight.state.note', text: bharatLoading ? 'Re-reading OCR and rebuilding guidance.' : bharatError ? 'The AI summary could not be refreshed.' : document.reviewRequired || (bharatInsight?.fraudSignals?.length || 0) > 0 ? 'Some extracted details should be checked manually.' : (bharatInsight?.claimSteps?.length || 0) > 0 ? 'The assistant prepared next steps from this invoice.' : 'Key invoice facts were captured successfully.' },
        { key: 'bharat.summary', text: formatInsightSummary(bharatInsight?.localizedSummary || bharatInsight?.consumerSummary, {
          productName: currentItem?.productName || document.title,
          sellerName: document.sellerName,
          purchaseDate: formatDisplayDate(currentItem?.purchaseDate) || currentItem?.purchaseDate,
          amount: currentInvoiceAmount,
        }) },
        { key: 'bharat.error', text: bharatError || '' },
        { key: 'serviceCenters.status', text: serviceCentersStatus || '' },
        { key: 'serviceCenters.error', text: serviceCentersError || '' },
      ]
      currentComplianceAlerts.forEach((entry) => entries.push(entry))
      currentNextActionItems.forEach((entry, index) => entries.push({ key: `claimAssistant.next.${index}`, text: entry }))
      currentVerificationItems.forEach((entry, index) => entries.push({ key: `claimAssistant.verify.${index}`, text: entry }))
      currentQuickQuestions.forEach((entry, index) => entries.push({ key: `chat.quick.${index}`, text: entry }))
      ;(claimPacket?.attachmentChecklist || []).forEach((entry, index) => entries.push({ key: `claimPacket.checklist.${index}`, text: entry }))
      ;(claimPacket?.timeline || []).forEach((entry, index) => entries.push({ key: `claimPacket.timeline.${index}`, text: entry }))
      if (claimPacket?.issueSummaryTemplate) {
        entries.push({ key: 'claimPacket.issueSummary.value', text: claimPacket.issueSummaryTemplate })
      }
      if (claimPacket?.emailTemplate) {
        entries.push({ key: 'claimPacket.email.value', text: claimPacket.emailTemplate })
      }

      const uniqueEntries = entries.filter((entry, index, all) => {
        if (!String(entry.text || '').trim()) return false
        return all.findIndex((candidate) => candidate.key === entry.key) === index
      })

      try {
        setPageTranslationLoading(true)
        const headers: HeadersInit = { 'Content-Type': 'application/json' }
        if (token) {
          headers.Authorization = `Bearer ${token}`
        }
        const response = await fetch('/api/ai/bharat/translate-batch', {
          method: 'POST',
          headers,
          body: JSON.stringify({
            texts: uniqueEntries.map((entry) => entry.text),
            targetLanguageCode: bharatLanguage,
            sourceLanguageCode: 'auto',
          }),
        })
        const payload = (await response.json().catch(() => null)) as BharatAITranslateBatchResponse | null
        if (!response.ok || !payload || !Array.isArray(payload.translations)) {
          if (!cancelled) {
            setPageTranslations({})
          }
          return
        }
        if (!cancelled) {
          setPageTranslations(buildTranslationMap(uniqueEntries, payload.translations))
        }
      } catch {
        if (!cancelled) {
          setPageTranslations({})
        }
      } finally {
        if (!cancelled) {
          setPageTranslationLoading(false)
        }
      }
    }

    void translatePage()
    return () => {
      cancelled = true
    }
  }, [
    assignmentActionError,
    bharatError,
    bharatInsight,
    bharatLanguage,
    bharatLoading,
    claimAssistant,
    claimPacket,
    document,
    nowMs,
    serviceCenters,
    serviceCentersError,
    serviceCentersStatus,
    token,
  ])
  useGsapReveal(rootRef, [isLoading, Boolean(document), showChat, messages.length])

  const handleDelete = async () => {
    if (!document || !window.confirm('Are you sure you want to delete this warranty?')) return
    setIsDeleting(true)
    try {
      await apiClient.delete(`/documents/${docId}`, { params: documentScopeParams })
      router.push(defaultHomeRoute)
    } catch (error) {
      console.error('Delete failed:', error)
      alert('Failed to delete. Please try again.')
    } finally {
      setIsDeleting(false)
    }
  }

  const loadCalendarLinks = useCallback(async () => {
    if (!document) return null
    try {
      setCalendarLoading(true)
      const payload = await apiClient.get<CalendarLinksResponse>(`/documents/${docId}/calendar-links`, {
        params: documentScopeParams,
      })
      setCalendarLinks(payload)
      return payload
    } catch (error) {
      console.error('Failed to load calendar links:', error)
      return null
    } finally {
      setCalendarLoading(false)
    }
  }, [docId, document, documentScopeParams])

  const loadClaimAssistant = useCallback(async () => {
    if (!document) return null
    try {
      setClaimAssistantLoading(true)
      const payload = await apiClient.get<ClaimAssistantResponse>(`/documents/${docId}/claim-assistant`, {
        params: documentScopeParams,
      })
      setClaimAssistant(payload)
      return payload
    } catch (error) {
      console.error('Failed to load claim assistant:', error)
      setClaimAssistant(null)
      return null
    } finally {
      setClaimAssistantLoading(false)
    }
  }, [docId, document, documentScopeParams])

  useEffect(() => {
    if (!document?.docId) {
      setClaimAssistant(null)
      return
    }
    void loadClaimAssistant()
  }, [document?.docId, loadClaimAssistant])

  const handleOpenGoogleCalendar = async () => {
    const payload = calendarLinks || (await loadCalendarLinks())
    if (!payload?.googleCalendarUrl) { alert('Warranty date is not available for calendar sync yet.'); return }
    window.open(payload.googleCalendarUrl, '_blank', 'noopener,noreferrer')
  }

  const handleDownloadIcs = async () => {
    const payload = calendarLinks || (await loadCalendarLinks())
    if (!payload?.icsDownloadUrl) { alert('Warranty date is not available for calendar sync yet.'); return }
    const query = new URLSearchParams(documentScopeParams || {}).toString()
    const target = `${payload.icsDownloadUrl}${payload.icsDownloadUrl.includes('?') ? '&' : '?'}${query}`
    window.open(target, '_blank', 'noopener,noreferrer')
  }

  const handleGenerateClaimPacket = async () => {
    try {
      setClaimPacketLoading(true)
      const payload = await apiClient.get<ClaimPacketResponse>(`/documents/${docId}/claim-packet`, {
        params: documentScopeParams,
      })
      setClaimPacket(payload)
    } catch (error) {
      console.error('Failed to generate claim packet:', error)
      alert('Unable to generate claim packet right now.')
    } finally {
      setClaimPacketLoading(false)
    }
  }

  const handleOpenSourceInvoice = async () => {
    try {
      setSourceUrlLoading(true)
      const payload = await apiClient.get<SourceUrlResponse>(`/documents/${docId}/source-url`, {
        params: { ...documentScopeParams, expiresIn: 900 },
      })
      if (!payload?.url) {
        alert('Source invoice is not available for this document.')
        return
      }
      window.open(payload.url, '_blank', 'noopener,noreferrer')
    } catch (error) {
      console.error('Failed to open source invoice:', error)
      alert('Source invoice is not available right now.')
    } finally {
      setSourceUrlLoading(false)
    }
  }

  const handleFindServiceCenters = async () => {
    try {
      setServiceCentersLoading(true)
      setServiceCentersError(null)
      setServiceCentersStatus('Checking your device location...')
      const location = await getCurrentLocation()
      const requestParams = {
        ...(documentScopeParams || {}),
        radiusKm: 25,
        limit: 4,
      }
      try {
        setServiceCentersStatus(location ? 'Searching nearby support...' : 'Searching official support options...')
        const payload = await apiClient.get<ServiceCentersResponse>(`/documents/${docId}/service-centers`, {
          params: {
            ...requestParams,
            userLatitude: location?.latitude,
            userLongitude: location?.longitude,
          },
          timeout: 12000,
        })
        setServiceCenters(payload)
      } catch (primaryError) {
        if (location) {
          setServiceCentersStatus('Retrying without device location...')
          const payload = await apiClient.get<ServiceCentersResponse>(`/documents/${docId}/service-centers`, {
            params: requestParams,
            timeout: 12000,
          })
          setServiceCenters(payload)
        } else {
          throw primaryError
        }
      }
    } catch (error) {
      console.error('Failed to load service centers:', error)
      setServiceCenters(null)
      setServiceCentersError('Unable to find nearby service centers right now.')
    } finally {
      setServiceCentersStatus(null)
      setServiceCentersLoading(false)
    }
  }

  const handleCopyText = async (value: string, label: string) => {
    const text = value.trim()
    if (!text) {
      alert(`${label} is empty right now.`)
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      alert(`${label} copied.`)
    } catch {
      alert(`Unable to copy ${label.toLowerCase()}.`)
    }
  }

  const handleRefreshBharatInsight = async () => {
    await runBharatEnrich({ includeSpeech: false, languageCode: bharatLanguage })
  }

  const handleGenerateBharatSpeech = async () => {
    await runBharatEnrich({ includeSpeech: true, languageCode: bharatLanguage })
  }

  const submitChatMessage = async (rawMessage: string) => {
    const trimmedMessage = rawMessage.trim()
    if (!trimmedMessage || isChatLoading || !document) return

    const userMessage: ChatMessage = { id: Date.now().toString(), role: 'user', content: trimmedMessage }
    setMessages((prev) => [...prev, userMessage])
    setIsChatLoading(true)

    try {
      if (!document.rawText?.trim()) {
        throw new Error('OCR text is not available for this document yet.')
      }
      const headers: HeadersInit = { 'Content-Type': 'application/json' }
      if (token) {
        headers.Authorization = `Bearer ${token}`
      }
      const response = await fetch('/api/ai/bharat/ask', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          question: trimmedMessage,
          ocrText: document.rawText,
          metadata: buildInvoiceAiMetadata(document, serviceCenters),
          targetLanguageCode: bharatLanguage,
        }),
      })
      const data = (await response.json().catch(() => null)) as BharatAIAskResponse | null
      if (!response.ok) {
        const errorMessage = typeof data?.error === 'string' ? data.error : 'Invoice AI answer failed.'
        throw new Error(errorMessage)
      }
      const assistantPayload = data || {}
      setMessages((prev) => [...prev, {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        content: String(assistantPayload.answer || 'No response available.'),
        supportPoints: Array.isArray(assistantPayload.supportPoints) ? assistantPayload.supportPoints.map((entry) => String(entry)) : [],
        missingInformation: Array.isArray(assistantPayload.missingInformation) ? assistantPayload.missingInformation.map((entry) => String(entry)) : [],
        confidenceNote: String(assistantPayload.confidenceNote || '').trim() || undefined,
      }])
    } catch (error) {
      const errorMessage = error instanceof Error && error.message ? error.message : 'Sorry, I encountered an error.'
      setMessages((prev) => [...prev, { id: (Date.now() + 1).toString(), role: 'assistant', content: errorMessage }])
    } finally {
      setIsChatLoading(false)
    }
  }

  const handleAskAI = async (event: React.FormEvent) => {
    event.preventDefault()
    const prompt = chatInput.trim()
    if (!prompt) return
    setChatInput('')
    await submitChatMessage(prompt)
  }

  const handleQuickQuestion = async (question: string) => {
    setChatInput('')
    await submitChatMessage(question)
  }

  if (isLoading) {
    return (
      <div className="dashboard-shell flex items-center justify-center">
        <span className="loading loading-spinner loading-lg text-blue-600"></span>
      </div>
    )
  }

  if (!document) {
    return (
      <div className="dashboard-shell flex items-center justify-center">
        <div className="text-center">
          <p className="text-slate-500 mb-4">Document not found</p>
          <button onClick={() => router.push(defaultHomeRoute)} className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 transition-all">
            {user?.userType === 'merchant' ? 'Back to Merchant Dashboard' : 'Back to Locker'}
          </button>
        </div>
      </div>
    )
  }

  const item = document.items[0]
  const invoiceNumber = getDocumentInvoiceNumber(document)
  const purchaseDate = getDocumentPurchaseDate(document)
  const invoiceAmount = getDocumentInvoiceAmount(document)
  const deadline = getDeadlineMeta(item?.warrantyEnd, nowMs)
  const compliance = document.compliance
  const complianceUi = complianceTone(compliance?.status)
  const displayAmount = formatCurrencyValue(invoiceAmount)
  const displayPurchaseDate = formatDisplayDate(purchaseDate)
  const displayWarrantyStart = formatDisplayDate(item?.warrantyStart)
  const displayWarrantyEnd = formatDisplayDate(item?.warrantyEnd)
  const insightSummary = formatInsightSummary(
    bharatInsight?.localizedSummary || bharatInsight?.consumerSummary,
    {
      productName: item?.productName || document.title,
      sellerName: document.sellerName,
      purchaseDate: displayPurchaseDate || purchaseDate,
      amount: invoiceAmount,
    }
  )
  const insightState = getInsightState({
    loading: bharatLoading,
    error: bharatError,
    reviewRequired: Boolean(document.reviewRequired),
    fraudSignalCount: bharatInsight?.fraudSignals?.length || 0,
    claimStepCount: bharatInsight?.claimSteps?.length || 0,
  })
  const nextActionItems = uniqueItems([
    ...(claimAssistant?.nextBestActions || []),
    ...(bharatInsight?.claimSteps || []),
    ...(claimPacket?.timeline || []),
  ]).slice(0, 5)
  const verificationItems = uniqueItems([
    ...(compliance?.alerts?.map((alert) => alert.message) || []),
    ...(bharatInsight?.fraudSignals || []),
    ...(document.reviewRequired
      ? [`Double-check extracted fields: ${(document.lowConfidenceFields || []).join(', ') || 'invoice details'}.`]
      : []),
  ]).slice(0, 5)
  const quickQuestions = uniqueItems([
    'Summarize what this invoice covers and how much warranty time is left.',
    'What proof should I keep ready before I contact support?',
    invoiceNumber ? `Check whether invoice ${invoiceNumber} has any claim or compliance risk.` : '',
    'What details are still missing or unclear in this invoice?',
  ]).slice(0, 4)
  const localizedDeadlineLabel = deadline
    ? translateUi(`deadline.label.${deadline.level}`, deadline.label)
    : translateUi('deadline.saved', 'Saved')
  const localizedDeadlineHint = deadline ? translateUi(`deadline.hint.${deadline.level}`, deadline.hint) : ''
  const localizedDeadlineCountdown = deadline
    ? deadline.level === 'expired'
      ? `${translateUi('deadline.ended', 'Ended')} ${deadline.countdown}`
      : `${deadline.countdown} ${translateUi('deadline.left', 'left')}`
    : 'N/A'
  const localizedInsightLabel = translateUi('insight.state.label', insightState.label)
  const localizedInsightNote = translateUi('insight.state.note', insightState.note)
  const localizedInsightSummary = translateUi('bharat.summary', insightSummary)
  const localizedBharatError = bharatError ? translateUi('bharat.error', bharatError) : null
  const localizedClaimReadinessSummary = document.claimReadiness?.summary
    ? translateUi('claimReadiness.summary', document.claimReadiness.summary)
    : ''
  const localizedComplianceLabel = translateUi('compliance.label', complianceUi.label)
  const localizedReviewDetail = translateUi(
    'review.detail',
    `Low confidence in: ${(document.lowConfidenceFields || []).join(', ') || 'invoice fields'}`
  )
  const localizedComplianceAlerts = compliance?.alerts?.length
    ? compliance.alerts.slice(0, 2).map((alert, index) => translateUi(`compliance.alert.${index}`, alert.message)).join(' | ')
    : ''
  const localizedComplianceAlertItems = (compliance?.alerts || []).slice(0, 3).map((alert, index) =>
    translateUi(`compliance.alertCard.${index}`, alert.message)
  )
  const localizedNextActionItems = nextActionItems.map((entry, index) => translateUi(`claimAssistant.next.${index}`, entry))
  const localizedVerificationItems = verificationItems.map((entry, index) => translateUi(`claimAssistant.verify.${index}`, entry))
  const localizedQuickQuestions = quickQuestions.map((entry, index) => translateUi(`chat.quick.${index}`, entry))
  const localizedSupportSummary = translateUi('support.summary', formatSupportSummary(serviceCenters))
  const localizedSupportGuidance = serviceCenters?.guidance
    ? translateUi('support.guidance', serviceCenters.guidance)
    : ''
  const localizedServiceCentersStatus = serviceCentersStatus
    ? translateUi('serviceCenters.status', serviceCentersStatus)
    : null
  const localizedServiceCentersError = serviceCentersError
    ? translateUi('serviceCenters.error', serviceCentersError)
    : null
  const localizedClaimPacketChecklist = (claimPacket?.attachmentChecklist || []).map((entry, index) =>
    translateUi(`claimPacket.checklist.${index}`, entry)
  )
  const localizedClaimPacketTimeline = (claimPacket?.timeline || []).map((entry, index) =>
    translateUi(`claimPacket.timeline.${index}`, entry)
  )
  const localizedIssueSummaryTemplate = claimPacket?.issueSummaryTemplate
    ? translateUi('claimPacket.issueSummary.value', claimPacket.issueSummaryTemplate)
    : ''
  const localizedEmailTemplate = claimPacket?.emailTemplate
    ? translateUi('claimPacket.email.value', claimPacket.emailTemplate)
    : ''
  const localizedGstFindings = (bharatInsight?.gstFindings || []).slice(0, 3).map((entry, index) =>
    translateUi(`bharat.gstFinding.${index}`, entry)
  )
  const localizedMerchantNotes = (bharatInsight?.merchantNotes || []).slice(0, 3).map((entry, index) =>
    translateUi(`bharat.merchantNote.${index}`, entry)
  )
  const localizedClaimStepSummary = localizedNextActionItems.slice(0, 3)
  const localizedAftercareItems = uniqueItems([
    ...localizedVerificationItems,
    ...(bharatInsight?.fraudSignals || []).map((entry, index) => translateUi(`bharat.fraudSignal.${index}`, entry)),
    ...(bharatInsight?.paymentReferences || []).map((entry, index) => translateUi(`bharat.paymentReference.${index}`, entry)),
  ]).slice(0, 3)
  const supportPreviewCenters = (serviceCenters?.centers || []).slice(0, 3)
  const isOutputUpdating = bharatLoading || pageTranslationLoading
  const isConsumerAssignment = Boolean(
    user?.userType === 'consumer' &&
    user?.userId &&
    document.assignedByMerchantId &&
    document.userId === user.userId
  )
  const assignmentStatus = document.assignmentStatus || 'assigned'
  const assignmentStatusClass =
    assignmentStatus === 'accepted'
      ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
      : assignmentStatus === 'escalated'
        ? 'border-amber-200 bg-amber-50 text-amber-700'
        : 'border-blue-200 bg-blue-50 text-blue-700'
  const assignmentStatusLabel =
    assignmentStatus === 'accepted'
      ? translateUi('assignment.accepted', 'Accepted')
      : assignmentStatus === 'escalated'
        ? translateUi('assignment.escalated', 'Escalated')
        : translateUi('assignment.pending', 'Awaiting your confirmation')
  const assignmentStatusNote =
    assignmentStatus === 'accepted'
      ? translateUi('assignment.acceptedNote', 'You already confirmed this invoice. The merchant audit trail now shows it as accepted.')
      : assignmentStatus === 'escalated'
        ? translateUi('assignment.escalatedNote', 'You escalated this assignment. The merchant workspace will see the escalation status.')
        : translateUi('assignment.synced', 'Merchant and consumer tracking stay synchronized after you respond here.')
  const assignmentActorLabel =
    document.assignedByMerchantName ||
    document.assignedByMerchantCustomId ||
    document.assignedByMerchantId ||
    translateUi('value.unknown', 'Unknown')
  const localizedAssignmentError = assignmentActionError
    ? translateUi('assignment.error', assignmentActionError)
    : null

  return (
    <div ref={rootRef} className="dashboard-shell">
      {/* Navbar */}
      <div data-gsap="hero" className="dashboard-navbar flex items-center justify-between px-4 py-3">
        <button onClick={() => router.back()} className="inline-flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium text-slate-600 hover:bg-blue-50 transition-colors">
          <ArrowLeft className="w-4 h-4" /> {translateUi('nav.back', 'Back')}
        </button>
        <button onClick={handleDelete} disabled={isDeleting} className="inline-flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 transition-colors">
          <Trash2 className="w-4 h-4" />
          {isDeleting ? translateUi('nav.deleting', 'Deleting...') : translateUi('nav.delete', 'Delete')}
        </button>
      </div>

      <div className="container mx-auto px-4 py-6 max-w-6xl">
        <section data-gsap="card" className="space-y-6">
          <div className="rounded-[32px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)] md:p-6">
            <div className="flex flex-col gap-5 xl:flex-row xl:items-start xl:justify-between">
              <div className="flex flex-1 flex-col gap-4 lg:flex-row lg:items-start">
                <div className="w-full max-w-[220px] shrink-0">
                  <div className="rounded-[28px] border border-slate-200 bg-[linear-gradient(180deg,#f8fbff_0%,#ffffff_100%)] p-3 shadow-[0_18px_34px_-28px_rgba(15,23,42,0.28)]">
                    <div className="mb-2 flex items-center justify-between gap-2 px-1">
                      <span className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">
                        {translateUi('page.productPreview', 'AI product preview')}
                      </span>
                    </div>
                    <ProductVisual
                      docId={document.docId}
                      alt={item?.productName || document.title || 'Product image'}
                      productImageAvailable={document.productImageAvailable}
                      productImageGeneratedAt={document.productImageGeneratedAt}
                      fallbackIcon={Package}
                      className="aspect-square w-full rounded-[24px] border border-slate-200 bg-white"
                      imageClassName="h-full w-full object-cover"
                    />
                  </div>
                </div>

                <div className="flex-1 space-y-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="inline-flex items-center rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-blue-700">
                      {translateUi('page.label', 'Invoice Assessment')}
                    </span>
                    {deadline ? (
                      <span className={`inline-flex rounded-full px-3 py-1 text-xs font-semibold ${deadline.badgeClass}`}>{localizedDeadlineLabel}</span>
                    ) : (
                      <span className="inline-flex rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-xs font-semibold text-blue-600">
                        {translateUi('status.saved', 'Saved')}
                      </span>
                    )}
                  </div>

                  <div className="space-y-2">
                    <h1 className="max-w-3xl text-2xl font-bold tracking-tight text-slate-950 md:text-4xl">
                      {item?.productName || document.title || 'Untitled'}
                    </h1>
                    <p className="max-w-3xl text-sm leading-7 text-slate-500 md:text-base">
                      {translateUi(
                        'page.summary',
                        `This invoice from ${document.sellerName || 'the seller'} records ${
                          item?.productName || 'the purchase'
                        }${displayPurchaseDate ? ` dated ${displayPurchaseDate}` : ''}${displayAmount ? ` for ${displayAmount}` : ''}.`
                      )}
                    </p>
                  </div>

                  <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <MetricPanel
                      label={translateUi('label.invoiceDate', 'Invoice Date')}
                      value={displayPurchaseDate || translateUi('value.notFound', 'Not found')}
                      tone="blue"
                    />
                    <MetricPanel
                      label={translateUi('label.invoiceAmount', 'Invoice Amount')}
                      value={displayAmount || translateUi('value.notFound', 'Not found')}
                      tone="blue"
                    />
                    <MetricPanel
                      label={translateUi('label.invoiceNo', 'Invoice No')}
                      value={invoiceNumber || translateUi('value.notFound', 'Not found')}
                      tone="slate"
                    />
                    <MetricPanel
                      label={translateUi('label.storeSeller', 'Supplier')}
                      value={document.sellerName || translateUi('value.unknown', 'Unknown')}
                      tone="slate"
                    />
                  </div>
                </div>
              </div>

              <div className="grid w-full gap-3 sm:grid-cols-2 xl:max-w-[320px] xl:grid-cols-1">
                <MetricPanel
                  label={translateUi('deadline.card.label', 'Time Left')}
                  value={deadline ? localizedDeadlineCountdown : translateUi('value.na', 'N/A')}
                  subtext={deadline ? localizedDeadlineHint : translateUi('deadline.saved', 'Saved to locker')}
                  tone={deadline?.level === 'expired' ? 'rose' : deadline?.level === 'action_now' ? 'amber' : 'blue'}
                />
                <MetricPanel
                  label={translateUi('claimReadiness.title', 'Claim Readiness')}
                  value={document.claimReadiness ? `${Math.round(document.claimReadiness.score * 100)}%` : translateUi('value.na', 'N/A')}
                  subtext={localizedClaimReadinessSummary || localizedInsightNote}
                  tone="emerald"
                />
              </div>
            </div>

            <div className="mt-5 grid gap-3">
              {document.reviewRequired ? (
                <div className="flex items-start gap-3 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3">
                  <AlertTriangle className="mt-0.5 h-4 w-4 text-amber-600" />
                  <div>
                    <p className="text-sm font-semibold text-amber-900">{translateUi('review.title', 'Verification recommended')}</p>
                    <p className="mt-1 text-xs leading-5 text-amber-700">{localizedReviewDetail}</p>
                  </div>
                </div>
              ) : null}

              {compliance ? (
                <div className={`flex items-start gap-3 rounded-2xl border px-4 py-3 ${complianceUi.alertClass}`}>
                  {compliance.status === 'pass' ? <BadgeCheck className="mt-0.5 h-4 w-4" /> : <AlertTriangle className="mt-0.5 h-4 w-4" />}
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-semibold">
                      {translateUi('compliance.gstEinvoice', 'GST + e-Invoice')}: {localizedComplianceLabel} ({compliance.score}/100)
                    </p>
                    <p className="mt-1 text-xs leading-5">
                      {translateUi('compliance.gstin', 'GSTIN')}: {compliance.gstin?.value || translateUi('compliance.notDetected', 'Not detected')}{' '}
                      {compliance.gstin?.valid_checksum ? translateUi('compliance.validated', '(validated)') : translateUi('compliance.verify', '(verify manually)')}
                    </p>
                    {localizedComplianceAlerts ? (
                      <p className="mt-1 text-xs leading-5">{translateUi('compliance.alerts', 'Alerts')}: {localizedComplianceAlerts}</p>
                    ) : null}
                  </div>
                </div>
              ) : null}

              {isConsumerAssignment ? (
                <div className="rounded-2xl border border-blue-200 bg-blue-50/70 px-4 py-4">
                  <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center rounded-full border border-blue-200 bg-white px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-blue-700">
                          {translateUi('assignment.label', 'Merchant Assignment')}
                        </span>
                        <span className={`inline-flex rounded-full border px-3 py-1 text-xs font-semibold ${assignmentStatusClass}`}>
                          {assignmentStatusLabel}
                        </span>
                      </div>
                      <p className="mt-3 text-sm font-semibold text-slate-900">
                        {translateUi('assignment.title', 'Assigned by merchant')}
                      </p>
                      <p className="mt-1 text-sm leading-6 text-slate-600">
                        {translateUi('assignment.assignedBy', 'Assigned by')}: {assignmentActorLabel}
                      </p>
                      <p className="mt-2 text-sm leading-6 text-slate-600">
                        {translateUi('assignment.description', 'Confirm that this invoice belongs in your locker or escalate it back to the merchant for correction.')}
                      </p>
                      <p className="mt-2 text-xs leading-5 text-slate-500">{assignmentStatusNote}</p>
                      {localizedAssignmentError ? (
                        <p className="mt-3 rounded-xl border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">
                          {localizedAssignmentError}
                        </p>
                      ) : null}
                    </div>

                    <div className="flex w-full shrink-0 flex-col gap-2 sm:w-auto">
                      <button
                        onClick={() => void handleAssignmentAction('accepted')}
                        disabled={assignmentActionLoading !== null || assignmentStatus === 'accepted'}
                        className="inline-flex items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                      >
                        {assignmentActionLoading === 'accepted' ? <span className="loading loading-spinner loading-xs"></span> : <BadgeCheck className="h-4 w-4" />}
                        {assignmentActionLoading === 'accepted'
                          ? translateUi('assignment.accepting', 'Accepting...')
                          : translateUi('assignment.accept', 'Accept Assignment')}
                      </button>
                      <button
                        onClick={() => void handleAssignmentAction('escalated')}
                        disabled={assignmentActionLoading !== null}
                        className="inline-flex items-center justify-center gap-2 rounded-xl border border-amber-200 bg-white px-4 py-2.5 text-sm font-semibold text-amber-700 transition hover:bg-amber-50 disabled:cursor-not-allowed disabled:opacity-60"
                      >
                        {assignmentActionLoading === 'escalated' ? <span className="loading loading-spinner loading-xs"></span> : <AlertTriangle className="h-4 w-4" />}
                        {assignmentActionLoading === 'escalated'
                          ? translateUi('assignment.escalating', 'Escalating...')
                          : translateUi('assignment.escalate', 'Escalate to Merchant')}
                      </button>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          </div>

          <div className="rounded-[32px] border border-slate-800 bg-[linear-gradient(135deg,#081127_0%,#0e2145_60%,#132857_100%)] p-5 text-white shadow-[0_30px_80px_rgba(15,23,42,0.28)] md:p-6">
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1.15fr)_320px]">
              <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="inline-flex items-center gap-2 rounded-full border border-cyan-400/20 bg-cyan-400/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] text-cyan-100">
                    <Bot className="h-3.5 w-3.5" />
                    {translateUi('bharat.badge', 'Bharat AI Assistant')}
                  </span>
                  <span className={`inline-flex items-center rounded-full border px-3 py-1 text-xs font-semibold ${insightState.className}`}>
                    {localizedInsightLabel}
                  </span>
                </div>

                <div className="space-y-2">
                  <p className="text-xl font-semibold tracking-tight text-white md:text-2xl">
                    {translateUi('bharat.heading', 'What this invoice means')}
                  </p>
                  <p className="max-w-3xl text-sm leading-7 text-slate-200 md:text-base">
                    {isOutputUpdating
                      ? translateUi('bharat.loading', 'Reviewing OCR text and building claim guidance.')
                      : localizedInsightSummary}
                  </p>
                  <p className="text-xs text-slate-400">{localizedInsightNote}</p>
                </div>

                <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                  <SummaryChip label={translateUi('label.invoiceDate', 'Invoice Date')} value={displayPurchaseDate || translateUi('value.notFound', 'Not found')} />
                  <SummaryChip label={translateUi('label.invoiceAmount', 'Invoice Amount')} value={displayAmount || translateUi('value.notFound', 'Not found')} />
                  <SummaryChip label={translateUi('label.invoiceNo', 'Invoice No')} value={invoiceNumber || translateUi('value.notFound', 'Not found')} />
                  <SummaryChip label={translateUi('label.storeSeller', 'Supplier')} value={document.sellerName || translateUi('value.unknown', 'Unknown')} />
                </div>
              </div>

              <div className="rounded-[28px] border border-white/10 bg-white/5 p-4 backdrop-blur-sm">
                <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-400">
                  {translateUi('bharat.personalize', 'Personal outputs')}
                </p>
                <div className="mt-3 grid gap-2">
                  <label className="input input-bordered input-sm flex w-full items-center gap-2 border-white/10 bg-white text-slate-900">
                    <Languages className="h-4 w-4 opacity-70" />
                    <select
                      value={bharatLanguage}
                      onChange={(event) => setBharatLanguage(event.target.value)}
                      className="w-full bg-transparent outline-none"
                    >
                      {BHARAT_LANGUAGE_OPTIONS.map((option) => (
                        <option key={option.code} value={option.code}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    onClick={handleRefreshBharatInsight}
                    disabled={isOutputUpdating || bharatSpeechLoading}
                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-white/15 bg-white/5 px-3 py-2 text-sm font-medium text-white transition-colors hover:bg-white/10 disabled:opacity-60"
                  >
                    {isOutputUpdating ? <span className="loading loading-spinner loading-xs"></span> : <RefreshCw className="h-4 w-4" />}
                    {translateUi('bharat.refresh', 'Refresh Insight')}
                  </button>
                </div>

                <div className="mt-4 rounded-2xl border border-white/10 bg-slate-950/35 p-3">
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
                    {translateUi('bharat.context', 'AI context')}
                  </p>
                  <div className="mt-2 space-y-1 text-xs text-slate-300">
                    <p>{translateUi('bharat.detected', 'Detected language')}: {bharatInsight?.sourceLanguageCode || 'unknown'}</p>
                    <p>{translateUi('bharat.output', 'Output language')}: {bharatInsight?.targetLanguageCode || bharatLanguage}</p>
                    <p>{translateUi('bharat.model', 'Base model')}: {formatModelLabel(bharatInsight?.modelUsed)}</p>
                  </div>
                </div>

                {bharatAudioSrc ? (
                  <div className="mt-4 rounded-2xl border border-white/10 bg-white/5 p-3">
                    <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">
                      {translateUi('bharat.audio.title', 'Audio Summary')}
                    </p>
                    <audio controls src={bharatAudioSrc} className="mt-3 h-10 w-full">
                      Your browser does not support audio playback.
                    </audio>
                  </div>
                ) : null}
              </div>
            </div>

            {localizedBharatError ? (
              <div className="mt-4 rounded-2xl border border-amber-300/30 bg-amber-300/10 px-4 py-3 text-sm text-amber-50">
                {localizedBharatError}
              </div>
            ) : null}

            <div className="mt-5 grid grid-cols-1 gap-3 lg:grid-cols-2">
              <InsightPanelCard
                title={translateUi('bharat.gst.title', 'GST And Invoice Checks')}
                description={translateUi('bharat.gst.desc', 'Tax, invoice, and filing signals to verify before claiming.')}
                items={localizedGstFindings.length ? localizedGstFindings : localizedComplianceAlertItems}
                emptyLabel={translateUi('bharat.gst.empty', 'No GST or invoice checks were generated.')}
                tone="info"
              />
              <InsightPanelCard
                title={translateUi('bharat.merchant.title', 'Merchant Context')}
                description={translateUi('bharat.merchant.desc', 'Seller context and invoice notes worth keeping nearby.')}
                items={localizedMerchantNotes}
                emptyLabel={translateUi('bharat.merchant.empty', 'No merchant notes were generated.')}
                tone="neutral"
              />
              <InsightPanelCard
                title={translateUi('bharat.actions.title', 'Action Summary')}
                description={translateUi('bharat.actions.desc', 'What to do next before you contact support or file a claim.')}
                items={localizedClaimStepSummary}
                emptyLabel={translateUi('bharat.actions.empty', 'No immediate next steps were generated.')}
                tone="action"
              />
              <InsightPanelCard
                title={translateUi('bharat.after.title', 'After Submission')}
                description={translateUi('bharat.after.desc', 'Things to watch after you submit or escalate this invoice.')}
                items={localizedAftercareItems}
                emptyLabel={translateUi('bharat.after.empty', 'No follow-up watch-outs were generated.')}
                tone="review"
              />
            </div>
          </div>

          <div data-gsap="card" className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-4">
            <button
              onClick={handleOpenSourceInvoice}
              disabled={sourceUrlLoading}
              data-gsap-hover="lift"
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50"
            >
              <ExternalLink className="h-4 w-4" />
              {sourceUrlLoading ? translateUi('actions.openingInvoice', 'Opening Invoice...') : translateUi('actions.openInvoice', 'Open Source Invoice')}
            </button>
            <button
              onClick={handleOpenGoogleCalendar}
              disabled={calendarLoading}
              data-gsap-hover="lift"
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50"
            >
              <CalendarPlus className="h-4 w-4" />
              {translateUi('actions.calendar', 'Add To Calendar')}
            </button>
            <button
              onClick={handleDownloadIcs}
              disabled={calendarLoading}
              data-gsap-hover="lift"
              className="inline-flex items-center justify-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50"
            >
              <Download className="h-4 w-4" />
              {translateUi('actions.exportReminder', 'Export Reminder')}
            </button>
            <button
              onClick={handleGenerateClaimPacket}
              disabled={claimPacketLoading}
              data-gsap-hover="lift"
              className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-3 py-2 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 transition-all hover:from-blue-700 hover:to-blue-800"
            >
              {claimPacketLoading ? <span className="loading loading-spinner loading-xs"></span> : null}
              {translateUi('actions.claimPacket', 'Prepare Claim Details')}
            </button>
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.25fr)_minmax(320px,0.75fr)]">
            <section data-gsap="card" className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('claimAssistant.label', 'Extraction Companion')}</p>
                  <h2 className="mt-1 text-xl font-semibold text-slate-900">{translateUi('claimAssistant.title', 'Action Plan')}</h2>
                  <p className="mt-1 text-sm text-slate-500">
                    {translateUi('claimAssistant.subtitle', 'Use this sequence before you contact support or file a claim.')}
                  </p>
                </div>
                {claimAssistant?.recommendedChannels?.length ? (
                  <div className="flex flex-wrap gap-2">
                    {claimAssistant.recommendedChannels.map((channel) => (
                      <span key={channel} className="inline-flex rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-medium text-blue-600">
                        {channel}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>

              {claimAssistantLoading ? (
                <p className="mt-4 text-sm text-slate-400">{translateUi('claimAssistant.loading', 'Loading claim playbook...')}</p>
              ) : (
                <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-2">
                  <InsightPanelCard
                    title={translateUi('claimAssistant.next.title', 'Do Next')}
                    description={translateUi('claimAssistant.next.desc', 'Immediate actions from the claim assistant and invoice AI.')}
                    items={localizedNextActionItems}
                    emptyLabel={translateUi('claimAssistant.next.empty', 'No action items were generated yet.')}
                    tone="action"
                  />
                  <InsightPanelCard
                    title={translateUi('claimAssistant.verify.title', 'Verify Before Claim')}
                    description={translateUi('claimAssistant.verify.desc', 'Check these points before you submit anything.')}
                    items={localizedVerificationItems}
                    emptyLabel={translateUi('claimAssistant.verify.empty', 'No blocking issues were flagged.')}
                    tone="review"
                  />
                </div>
              )}
            </section>

            <section data-gsap="card" className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('support.label', 'Need Assistance?')}</p>
                  <h2 className="mt-1 text-xl font-semibold text-slate-900">{translateUi('support.title', 'Nearby Support')}</h2>
                  <p className="mt-1 text-sm leading-6 text-slate-500">
                    {serviceCenters ? localizedSupportSummary : translateUi('support.description', 'Find brand-authorized service locations using your current device location.')}
                  </p>
                </div>
                <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-50 text-blue-600">
                  <Phone className="h-5 w-5" />
                </div>
              </div>

              <button
                onClick={handleFindServiceCenters}
                disabled={serviceCentersLoading}
                className="mt-4 inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-3 py-2 text-sm font-semibold text-white transition-all hover:from-blue-700 hover:to-blue-800 disabled:opacity-60"
              >
                <MapPin className="h-4 w-4" />
                {serviceCentersLoading ? translateUi('support.searching', 'Searching...') : translateUi('support.find', 'Find Centers')}
              </button>

              {localizedServiceCentersStatus ? (
                <p className="mt-3 text-xs text-slate-400">{localizedServiceCentersStatus}</p>
              ) : null}
              {localizedServiceCentersError ? (
                <p className="mt-3 rounded-2xl border border-red-200 bg-red-50 px-3 py-2 text-xs leading-5 text-red-700">{localizedServiceCentersError}</p>
              ) : null}
              {!supportPreviewCenters.length && localizedSupportGuidance ? (
                <p className="mt-3 rounded-2xl border border-slate-200 bg-slate-50 px-3 py-3 text-sm leading-6 text-slate-500">{localizedSupportGuidance}</p>
              ) : null}

              <div className="mt-4 space-y-3">
                {supportPreviewCenters.length ? (
                  supportPreviewCenters.map((center, index) => (
                    <SupportCenterCard key={`${center.name}-${index}`} center={center} index={index} translateLabel={translateUi} />
                  ))
                ) : (
                  <div className="rounded-2xl border border-dashed border-slate-200 bg-slate-50 px-4 py-6 text-sm leading-6 text-slate-400">
                    {translateUi('support.idle', 'Search when you need repair or warranty service help.')}
                  </div>
                )}
              </div>
            </section>
          </div>

          {claimPacket ? (
            <details data-gsap="panel" className="group rounded-[28px] border border-slate-200 bg-white shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
              <summary className="flex cursor-pointer list-none flex-col gap-3 px-5 py-4 md:flex-row md:items-center md:justify-between">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.16em] text-blue-600">{translateUi('claimPacket.label', 'Claim Details')}</p>
                  <h2 className="mt-1 text-lg font-semibold text-slate-900">{translateUi('claimPacket.title', 'Prepared For You')}</h2>
                  <p className="mt-1 text-sm text-slate-500">
                    {translateUi('claimPacket.generatedAt', 'Generated at')} {new Date(claimPacket.generatedAt).toLocaleString()}
                  </p>
                </div>
                <div className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-semibold text-slate-600 transition-colors group-open:bg-blue-50 group-open:text-blue-700">
                  {translateUi('claimPacket.expand', 'Open prepared details')}
                </div>
              </summary>

              <div className="border-t border-slate-200 px-5 py-5">
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={() => handleCopyText(localizedIssueSummaryTemplate || claimPacket.issueSummaryTemplate, 'Issue summary')}
                    className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition-colors hover:bg-slate-50"
                  >
                    <Copy className="h-4 w-4" />
                    {translateUi('claimPacket.copySummary', 'Copy Summary')}
                  </button>
                  <button
                    onClick={() => handleCopyText(localizedEmailTemplate || claimPacket.emailTemplate, 'Claim email')}
                    className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-3 py-2 text-sm font-semibold text-white transition-all hover:from-blue-700 hover:to-blue-800"
                  >
                    <Copy className="h-4 w-4" />
                    {translateUi('claimPacket.copyEmail', 'Copy Email')}
                  </button>
                </div>

                <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.9fr)]">
                  <div className="grid gap-4 md:grid-cols-2">
                    <InsightPanelCard
                      title={translateUi('claimPacket.checklist.title', 'Checklist')}
                      description={translateUi('claimPacket.checklist.desc', 'Documents and proof to keep ready before submitting.')}
                      items={localizedClaimPacketChecklist}
                      emptyLabel={translateUi('claimPacket.checklist.empty', 'No checklist items were generated.')}
                      tone="action"
                    />
                    <InsightPanelCard
                      title={translateUi('claimPacket.timeline.title', 'Timeline')}
                      description={translateUi('claimPacket.timeline.desc', 'Suggested order for the support or claim workflow.')}
                      items={localizedClaimPacketTimeline}
                      emptyLabel={translateUi('claimPacket.timeline.empty', 'No timeline steps were generated.')}
                      tone="info"
                    />
                    <TemplateCard
                      title={translateUi('claimPacket.issueSummary.title', 'Issue Summary')}
                      description={translateUi('claimPacket.issueSummary.desc', 'Use this to describe the problem clearly to support.')}
                      value={localizedIssueSummaryTemplate || claimPacket.issueSummaryTemplate}
                    />
                    <TemplateCard
                      title={translateUi('claimPacket.email.title', 'Email Draft')}
                      description={translateUi('claimPacket.email.desc', 'Use this as the first draft for your claim email.')}
                      value={localizedEmailTemplate || claimPacket.emailTemplate}
                    />
                  </div>

                  <div className="rounded-3xl border border-slate-200 bg-slate-50 p-4">
                    <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('claimPacket.facts', 'Claim Facts')}</p>
                    <div className="mt-3 space-y-3">
                      {Object.entries(claimPacket.facts || {}).slice(0, 8).map(([key, value]) => (
                        <div key={key} className="flex items-start justify-between gap-3 border-b border-slate-200 pb-3 last:border-b-0 last:pb-0">
                          <span className="text-xs uppercase tracking-[0.14em] text-slate-400">{key.replace(/_/g, ' ')}</span>
                          <span className="max-w-[60%] break-words text-right text-sm font-medium text-slate-900">{String(value ?? 'N/A')}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </details>
          ) : null}

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(320px,0.85fr)]">
            <section data-gsap="card" className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
              <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('sections.productLabel', 'Product Information')}</p>
              <h2 className="mt-1 text-xl font-semibold text-slate-900">{translateUi('sections.product', 'Product Information')}</h2>
              <div className="mt-5 grid grid-cols-1 gap-5 md:grid-cols-2">
                <InfoItem icon={Package} label={translateUi('label.productName', 'Product Name')} value={item?.productName || translateUi('value.na', 'N/A')} />
                <InfoItem icon={Package} label={translateUi('label.brandModel', 'Brand/Model')} value={item?.model || translateUi('value.na', 'N/A')} />
                <InfoItem icon={Hash} label={translateUi('label.category', 'Category')} value={document.category || translateUi('value.others', 'Others')} />
                <InfoItem icon={Hash} label={translateUi('label.serialNumber', 'Serial Number')} value={item?.serialNumber || translateUi('value.na', 'N/A')} />
                <InfoItem icon={Hash} label={translateUi('label.invoiceNo', 'Invoice No')} value={invoiceNumber || translateUi('value.na', 'N/A')} />
                <InfoItem icon={Store} label={translateUi('label.storeSeller', 'Invoice Vendor')} value={document.sellerName || translateUi('value.na', 'N/A')} />
              </div>
            </section>

            <div className="space-y-4">
              <section data-gsap="card" className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('sections.purchaseLabel', 'Purchase Information')}</p>
                <h2 className="mt-1 text-xl font-semibold text-slate-900">{translateUi('sections.purchase', 'Purchase Information')}</h2>
                <div className="mt-5 grid grid-cols-1 gap-5">
                  <InfoItem icon={Calendar} label={translateUi('label.purchaseDate', 'Purchase Date')} value={displayPurchaseDate || purchaseDate || translateUi('value.na', 'N/A')} />
                  <InfoItem icon={DollarSign} label={translateUi('label.amount', 'Amount')} value={displayAmount || translateUi('value.na', 'N/A')} />
                </div>
              </section>

              <section data-gsap="card" className="rounded-[28px] border border-slate-200 bg-white p-5 shadow-[0_18px_40px_rgba(15,23,42,0.06)]">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('sections.warrantyLabel', 'Warranty Information')}</p>
                <h2 className="mt-1 text-xl font-semibold text-slate-900">{translateUi('sections.warranty', 'Warranty Information')}</h2>
                <div className="mt-5 grid grid-cols-1 gap-5">
                  <InfoItem icon={ShieldCheck} label={translateUi('label.warrantyPeriod', 'Warranty Period')} value={item?.warrantyMonths ? `${item.warrantyMonths} months` : translateUi('value.na', 'N/A')} />
                  <InfoItem icon={Calendar} label={translateUi('label.warrantyStart', 'Warranty Start')} value={displayWarrantyStart || item?.warrantyStart || translateUi('value.na', 'N/A')} />
                  <InfoItem icon={Calendar} label={translateUi('label.warrantyEnd', 'Warranty End')} value={displayWarrantyEnd || item?.warrantyEnd || translateUi('value.na', 'N/A')} />
                  <InfoItem icon={ShieldCheck} label={translateUi('label.deadlineStatus', 'Deadline Status')} value={localizedDeadlineLabel || translateUi('value.na', 'N/A')} />
                  <InfoItem icon={ShieldCheck} label={translateUi('label.timeRemaining', 'Time Remaining')} value={deadline ? localizedDeadlineCountdown : translateUi('value.na', 'N/A')} />
                </div>
              </section>
            </div>
          </div>
        </section>

        {/* AI Chat Section */}
        <div data-gsap="panel" className="mt-6">
          <button
            onClick={() => {
              setShowChat(!showChat)
              if (!showChat && messages.length === 0) {
                setMessages([{
                  id: '1',
                  role: 'assistant',
                  content: translateUi('chat.greeting', `Hi! I can answer questions about your ${(item?.productName || 'warranty').split('|')[0].replace(/[\*\s\:\-\[\]]+$/, '').trim()}.`),
                }])
              }
            }}
            data-gsap-hover="lift"
            className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 py-3 px-4 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 hover:from-blue-700 hover:to-blue-800 transition-all"
          >
            <MessageSquare className="w-5 h-5" />
            {showChat ? translateUi('chat.hide', 'Hide AI Chat') : translateUi('chat.show', 'Ask AI About This Warranty')}
          </button>

          {showChat && (
            <div className="dashboard-card mt-4 overflow-hidden">
              {/* Chat Header */}
              <div className="bg-gradient-to-r from-blue-600 to-blue-700 text-white p-3 flex items-center gap-3">
                <div className="w-9 h-9 rounded-full bg-white/10 border border-white/20 flex items-center justify-center">
                  <Bot className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="font-bold text-sm">{translateUi('chat.title', 'Warranty Assistant')}</h3>
                  <p className="text-xs opacity-70">{translateUi('chat.subtitle', 'Grounded on your document and powered by SafeBill AI')}</p>
                </div>
              </div>

              <div className="border-b border-slate-200 bg-slate-50 px-4 py-3">
                <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('chat.quick', 'Quick Questions')}</p>
                <div className="mt-2 flex flex-wrap gap-2">
                  {localizedQuickQuestions.map((question, index) => (
                    <button
                      key={`${question}-${index}`}
                      type="button"
                      onClick={() => void handleQuickQuestion(quickQuestions[index] || question)}
                      disabled={isChatLoading}
                      className="inline-flex rounded-xl border border-slate-200 bg-white px-2 py-1 text-xs font-medium text-slate-700 hover:bg-blue-50 hover:border-blue-200 hover:text-blue-700 disabled:opacity-50 transition-colors"
                    >
                      {question}
                    </button>
                  ))}
                </div>
              </div>

              {/* Messages */}
              <div className="h-[360px] overflow-y-auto p-4 space-y-3">
                {messages.map((message) => (
                  <div key={message.id} className={`flex gap-3 ${message.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}>
                    <div className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${message.role === 'user' ? 'bg-blue-100 border border-blue-200' : 'bg-slate-100 border border-slate-200'}`}>
                      {message.role === 'user' ? <User className="w-4 h-4 text-blue-600" /> : <Bot className="w-4 h-4 text-slate-500" />}
                    </div>
                    <div className={`max-w-[82%] space-y-2`}>
                      <div className={`rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${message.role === 'user' ? 'bg-gradient-to-r from-blue-600 to-blue-700 text-white rounded-tr-none' : 'bg-slate-100 text-slate-800 rounded-tl-none'}`}>
                        {message.content}
                      </div>
                      {message.role === 'assistant' && message.sources?.length ? (
                        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
                          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('chat.grounding', 'Grounding')}</p>
                          <div className="mt-2 space-y-2">
                            {message.sources.slice(0, 2).map((source, index) => (
                              <div key={`${source.docId}-${index}`} className="text-xs text-slate-600">
                                <p className="font-semibold text-slate-700">Document {source.docId || translateUi('chat.reference', 'reference')}</p>
                                <p className="mt-1 line-clamp-3">{source.chunk}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : null}
                      {message.role === 'assistant' && message.supportPoints?.length ? (
                        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2">
                          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-emerald-700">{translateUi('chat.supportPoints', 'Grounded Points')}</p>
                          <ul className="mt-2 space-y-1">
                            {message.supportPoints.map((point, index) => (
                              <li key={`${message.id}-support-${index}`} className="text-xs leading-5 text-emerald-900">
                                {point}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {message.role === 'assistant' && message.missingInformation?.length ? (
                        <div className="rounded-xl border border-amber-200 bg-amber-50 px-3 py-2">
                          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-amber-700">{translateUi('chat.missing', 'Still Not Visible On Invoice')}</p>
                          <ul className="mt-2 space-y-1">
                            {message.missingInformation.map((point, index) => (
                              <li key={`${message.id}-missing-${index}`} className="text-xs leading-5 text-amber-900">
                                {point}
                              </li>
                            ))}
                          </ul>
                        </div>
                      ) : null}
                      {message.role === 'assistant' && message.confidenceNote ? (
                        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
                          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">{translateUi('chat.note', 'AI note')}</p>
                          <p className="mt-1 text-xs leading-5 text-slate-600">{message.confidenceNote}</p>
                        </div>
                      ) : null}
                      {message.role === 'assistant' && message.serviceCenters?.length ? (
                        <div className="rounded-xl border border-slate-200 bg-white px-3 py-2">
                          <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">Suggested Support</p>
                          <div className="mt-2 grid gap-2">
                            {message.serviceCenters.slice(0, 2).map((center, index) => (
                              <div key={`${center.name}-${index}`} className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
                                <p className="text-xs font-semibold text-slate-900">{center.name}</p>
                                <p className="mt-1 text-xs text-slate-500">{center.address}</p>
                                <div className="mt-2 flex flex-wrap gap-2">
                                  {center.phone ? (
                                    <a href={`tel:${center.phone}`} className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-100">
                                      <Phone className="w-3 h-3" /> {translateUi('support.call', 'Call')}
                                    </a>
                                  ) : null}
                                  {center.map_url ? (
                                    <a href={center.map_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-100">
                                      <ExternalLink className="w-3 h-3" /> {translateUi('support.openMap', 'Open Map')}
                                    </a>
                                  ) : null}
                                </div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : null}
                    </div>
                  </div>
                ))}
                {isChatLoading && (
                  <div className="flex gap-3">
                    <div className="w-8 h-8 rounded-full bg-slate-100 border border-slate-200 flex items-center justify-center">
                      <Bot className="w-4 h-4 text-slate-500" />
                    </div>
                    <div className="rounded-2xl rounded-tl-none bg-slate-100 px-4 py-2.5">
                      <span className="loading loading-dots loading-sm"></span>
                    </div>
                  </div>
                )}
                <div ref={messagesEndRef} />
              </div>

              {/* Input */}
              <form onSubmit={handleAskAI} className="p-3 border-t border-slate-200">
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={chatInput}
                    onChange={(e) => setChatInput(e.target.value)}
                    placeholder={translateUi('chat.placeholder', 'Ask about this warranty...')}
                    className="dashboard-input flex-1"
                  />
                  <button type="submit" disabled={!chatInput.trim() || isChatLoading} className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 text-white shadow-lg shadow-blue-200/60 hover:from-blue-700 hover:to-blue-800 disabled:opacity-60 transition-all">
                    <Send className="w-3.5 h-3.5" />
                  </button>
                </div>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function MetricPanel({
  label,
  value,
  subtext,
  tone,
}: {
  label: string
  value: string
  subtext?: string
  tone: 'blue' | 'slate' | 'emerald' | 'amber' | 'rose'
}) {
  const toneMap = {
    blue: 'border-blue-100 bg-blue-50/70 text-blue-700',
    slate: 'border-slate-200 bg-slate-50 text-slate-700',
    emerald: 'border-emerald-100 bg-emerald-50 text-emerald-700',
    amber: 'border-amber-100 bg-amber-50 text-amber-700',
    rose: 'border-rose-100 bg-rose-50 text-rose-700',
  } as const

  return (
    <div className={`rounded-2xl border px-4 py-3 ${toneMap[tone]}`}>
      <p className="text-[11px] font-semibold uppercase tracking-[0.16em] opacity-75">{label}</p>
      <p className="mt-2 text-base font-semibold leading-6 text-slate-900 break-words">{value}</p>
      {subtext ? <p className="mt-1 text-xs leading-5 text-slate-500">{subtext}</p> : null}
    </div>
  )
}

function InfoItem({ icon: Icon, label, value }: { icon: React.ComponentType<{ className?: string }>; label: string; value: string }) {
  const isAvailable = value !== 'N/A'
  return (
    <div data-gsap="list-item" className="flex items-start gap-3">
      <Icon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${isAvailable ? 'text-blue-600' : 'text-slate-300'}`} />
      <div className="min-w-0">
        <p className="text-xs text-slate-400 font-medium uppercase tracking-wide">{label}</p>
        <p className={`text-base md:text-lg font-semibold break-words mt-0.5 ${isAvailable ? 'text-slate-900' : 'text-slate-300 italic'}`}>{value}</p>
      </div>
    </div>
  )
}

function SummaryChip({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-white/5 p-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-400">{label}</p>
      <p className="mt-1 text-sm font-semibold text-white break-words">{value}</p>
    </div>
  )
}

function InsightPanelCard({
  title,
  description,
  items,
  emptyLabel,
  tone,
}: {
  title: string
  description: string
  items: string[]
  emptyLabel: string
  tone: 'action' | 'review' | 'info' | 'neutral'
}) {
  const toneStyle =
    tone === 'action'
      ? 'border-emerald-200 bg-emerald-50'
      : tone === 'review'
        ? 'border-amber-200 bg-amber-50'
        : tone === 'info'
          ? 'border-sky-200 bg-sky-50'
          : 'border-slate-200 bg-white'

  const titleColor =
    tone === 'action' ? 'text-emerald-700'
      : tone === 'review' ? 'text-amber-700'
        : tone === 'info' ? 'text-sky-700'
          : 'text-slate-600'

  return (
    <div className={`rounded-2xl border p-4 ${toneStyle}`}>
      <p className={`text-xs font-semibold uppercase tracking-[0.16em] ${titleColor}`}>{title}</p>
      <p className="mt-1 text-sm text-slate-500">{description}</p>
      {items.length ? (
        <ul className="mt-3 space-y-2">
          {items.slice(0, 4).map((entry, index) => (
            <li key={`${title}-${index}`} className="text-sm leading-6 text-slate-700">
              {entry}
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm italic text-slate-400">{emptyLabel}</p>
      )}
    </div>
  )
}

function TemplateCard({ title, description, value }: { title: string; description: string; value: string }) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-4">
      <p className="text-xs font-semibold uppercase tracking-[0.16em] text-slate-400">{title}</p>
      <p className="mt-1 text-sm text-slate-500">{description}</p>
      <div className="mt-3 rounded-xl bg-slate-50 border border-slate-100 p-4 text-sm leading-6 text-slate-700 whitespace-pre-wrap">
        {value || 'Not available.'}
      </div>
    </div>
  )
}

function SupportCenterCard({
  center,
  index,
  translateLabel,
}: {
  center: ServiceCenterItem
  index: number
  translateLabel: (key: string, fallback: string) => string
}) {
  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="text-sm font-semibold text-slate-900">{center.name}</p>
            <span className="inline-flex rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-500">
              {formatSupportSource(center.source)}
            </span>
          </div>
          <p className="mt-1 text-xs leading-5 text-slate-500 break-words">{center.address}</p>
        </div>
        <span className="inline-flex h-7 min-w-7 items-center justify-center rounded-full bg-blue-600 px-2 text-[11px] font-semibold text-white">
          {index + 1}
        </span>
      </div>

      {formatSupportMeta(center).length ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {formatSupportMeta(center).map((item) => (
            <span key={`${center.name}-${item}`} className="inline-flex rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-500">
              {item}
            </span>
          ))}
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap gap-2">
        {center.phone ? (
          <a href={`tel:${center.phone}`} className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-100">
            <Phone className="h-3 w-3" /> {translateLabel('support.call', 'Call')}
          </a>
        ) : null}
        {center.website ? (
          <a href={center.website} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-100">
            <Globe className="h-3 w-3" /> {translateLabel('support.website', 'Website')}
          </a>
        ) : null}
        {center.map_url ? (
          <a href={center.map_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 rounded-lg bg-blue-600 px-2.5 py-1 text-xs font-medium text-white transition-colors hover:bg-blue-700">
            <ExternalLink className="h-3 w-3" /> {translateLabel('support.openMap', 'Open Map')}
          </a>
        ) : null}
      </div>
    </div>
  )
}
