'use client'

import { useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import Image from 'next/image'
import {
  AlertTriangle,
  ArrowLeft,
  Camera,
  Check,
  DollarSign,
  FileText,
  Hash,
  Package,
  ScanLine,
  ShieldCheck,
  Store,
  Upload,
  Zap,
} from 'lucide-react'
import { useAuthStore } from '@/lib/store/auth-store'
import { useGsapReveal } from '@/lib/gsap-helpers'

interface ScanResult {
  docId: string
  title: string
  sellerName: string
  category: string
  items: Array<{
    productName: string
    model: string
    serialNumber: string
    invoiceNo: string
    purchaseDate: string
    purchasePrice: number | null
    warrantyMonths: number | null
    warrantyStart: string
    warrantyEnd: string
  }>
}

interface ScanApiResponse {
  pending?: boolean
  jobId?: string
  status?: string
  document?: {
    docId: string
    title: string
    details?: {
      productName?: string
      brand?: string
      category?: string
      amount?: string
      purchaseDate?: string
      warrantyPeriod?: string
      warrantyStart?: string
      warrantyEnd?: string
      serialNumber?: string
      invoiceNumber?: string
      store?: string
    }
  }
  error?: string
}

function toReadableError(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>
    const candidates = [record.message, record.error, record.detail, record.reason]
    for (const candidate of candidates) {
      if (typeof candidate === 'string' && candidate.trim()) {
        return candidate
      }
    }
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return 'Scan failed. Please try again.'
}

function formatRupee(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null
  const formatted = new Intl.NumberFormat('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)
  return `Rs ${formatted}`
}

export function ScanScreen() {
  const router = useRouter()
  const { user, token } = useAuthStore()
  const rootRef = useRef<HTMLDivElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<string | null>(null)
  const [scanResult, setScanResult] = useState<ScanResult | null>(null)
  const [isScanning, setIsScanning] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showManualFallback, setShowManualFallback] = useState(false)
  const ocrMode = 'hybrid'
  const [manualBillId, setManualBillId] = useState('')
  const [manualVendor, setManualVendor] = useState('')
  const [manualPurchaseDate, setManualPurchaseDate] = useState('')
  const [manualTotalAmount, setManualTotalAmount] = useState('')
  const cameraInputRef = useRef<HTMLInputElement | null>(null)
  const uploadInputRef = useRef<HTMLInputElement | null>(null)

  useGsapReveal(rootRef, [Boolean(file), Boolean(scanResult), isScanning, error])

  const applyScanResult = (scanned: NonNullable<ScanApiResponse['document']>) => {
    const details = scanned.details || {}
    const parsedAmount = details.amount ? Number.parseFloat(details.amount) : null
    const parsedWarrantyMonths = details.warrantyPeriod
      ? Number.parseInt(String(details.warrantyPeriod).split(' ', 1)[0], 10) || null
      : null

    setScanResult({
      docId: scanned.docId,
      title: scanned.title,
      sellerName: details.store || '',
      category: details.category || 'Others',
      items: [
        {
          productName: details.productName || scanned.title || '',
          model: details.brand || '',
          serialNumber: details.serialNumber || '',
          invoiceNo: details.invoiceNumber || '',
          purchaseDate: details.purchaseDate || '',
          purchasePrice:
            parsedAmount !== null && Number.isFinite(parsedAmount) ? parsedAmount : null,
          warrantyMonths: parsedWarrantyMonths,
          warrantyStart: details.warrantyStart || '',
          warrantyEnd: details.warrantyEnd || '',
        },
      ],
    })
  }

  const pollAsyncScanJob = async (jobId: string, headers: HeadersInit) => {
    const deadline = Date.now() + 120000
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 2500))
      const response = await fetch(`/api/scan?jobId=${encodeURIComponent(jobId)}`, {
        method: 'GET',
        headers,
        credentials: 'include',
      })
      const data = (await response.json().catch(() => null)) as ScanApiResponse | null
      if (response.status === 202 || data?.pending) {
        continue
      }
      if (!response.ok) {
        throw new Error(toReadableError(data?.error || data))
      }
      return data
    }
    throw new Error('Extraction is still processing. Retry in a few seconds.')
  }

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const selected = event.target.files?.[0]
    if (!selected) return
    setFile(selected)
    setScanResult(null)
    setError(null)

    if (selected.type.startsWith('image/')) {
      const reader = new FileReader()
      reader.onload = (e) => setPreview(e.target?.result as string)
      reader.readAsDataURL(selected)
    } else {
      setPreview(null)
    }
  }

  const handleScan = async (useManualFields = false) => {
    if (!file) return
    setIsScanning(true)
    setError(null)

    try {
      const formData = new FormData()
      formData.append('file', file)
      if (user?.userId && user.userId.trim()) {
        formData.append('userId', user.userId.trim())
      }
      if (user?.email) formData.append('consumerEmail', user.email)
      formData.append('ocrMode', ocrMode)
      if (useManualFields) {
        if (manualBillId.trim()) formData.append('billId', manualBillId.trim())
        if (manualVendor.trim()) formData.append('vendor', manualVendor.trim())
        if (manualPurchaseDate.trim()) formData.append('purchaseDate', manualPurchaseDate.trim())
        if (manualTotalAmount.trim()) formData.append('totalAmount', manualTotalAmount.trim())
      }

      const headers: HeadersInit = {}
      if (token) {
        headers.Authorization = `Bearer ${token}`
      }

      let clientOcrText = ''
      const isImageFile = file.type.startsWith('image/')
      if (isImageFile) {
        try {
          const { createWorker } = await import('tesseract.js')
          const worker = await createWorker('eng')
          const ret = await worker.recognize(file)
          clientOcrText = ret.data.text || ''
          await worker.terminate()
        } catch (err) {
          console.warn('Client Tesseract OCR skipped:', err)
        }
      }
      if (clientOcrText.trim()) {
        formData.append('ocrText', clientOcrText.trim())
      }

      const response = await fetch('/api/scan', {
        method: 'POST',
        body: formData,
        headers,
        credentials: 'include',
      })
      let data = (await response.json().catch(() => null)) as ScanApiResponse | null

      if (response.status === 202 || data?.pending) {
        const jobId = String(data?.jobId || '').trim()
        if (!jobId) {
          throw new Error('Async scan job is missing an id.')
        }
        data = await pollAsyncScanJob(jobId, headers)
      }

      if (!response.ok) {
        let message = toReadableError(data?.error || data)
        if (message === 'Scan failed. Please try again.') {
          if (response.status === 504 || response.status === 502) {
            message = 'Server timed out or is warming up. Please wait 10 seconds and click Start Extraction again.'
          } else if (response.status === 401) {
            message = 'Session expired or unauthorized. Please log out and sign in again.'
          } else {
            message = `Scan request failed with status ${response.status}. Please try again.`
          }
        }
        if (message.includes('Unable to extract readable text from this image')) {
          setShowManualFallback(true)
        }
        throw new Error(message)
      }
      const scanned = data?.document
      if (!scanned?.docId) {
        throw new Error('Scan response is missing document payload.')
      }

      applyScanResult(scanned)
      setShowManualFallback(false)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Scan failed.')
    } finally {
      setIsScanning(false)
    }
  }

  const resetScan = () => {
    setFile(null)
    setPreview(null)
    setScanResult(null)
    setError(null)
    setShowManualFallback(false)
    setManualBillId('')
    setManualVendor('')
    setManualPurchaseDate('')
    setManualTotalAmount('')
    if (uploadInputRef.current) uploadInputRef.current.value = ''
    if (cameraInputRef.current) cameraInputRef.current.value = ''
  }

  const openDocument = () => {
    if (!scanResult?.docId) return
    router.push(`/document/${scanResult.docId}`)
  }

  const item = scanResult?.items?.[0]

  return (
    <div ref={rootRef} className="dashboard-shell">
      <div className="mx-auto max-w-5xl px-4 pb-10 pt-6 md:px-6">
        <header data-gsap="hero" className="mb-6 flex items-center justify-between dashboard-card p-3 md:p-4">
          <div className="flex items-center gap-3">
            <button
              onClick={() => router.push('/locker')}
              className="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-slate-200 bg-white transition-all hover:bg-blue-50 hover:border-blue-200"
            >
              <ArrowLeft className="h-4 w-4 text-slate-600" />
            </button>
            <div>
              <h1 className="text-xl font-semibold text-slate-900 md:text-2xl">Scan Invoice</h1>
              <p className="text-xs text-slate-400 md:text-sm">AI extraction for warranty details</p>
            </div>
          </div>
        </header>

        <div className="grid gap-5 lg:grid-cols-2">
          <section data-gsap="card" className="dashboard-card p-4">
            {!file ? (
              <div className="py-10 text-center">
                <div className="mx-auto mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-blue-50 border border-blue-100">
                  <ScanLine className="h-8 w-8 text-blue-500" />
                </div>
                <h2 className="text-lg font-semibold text-slate-900">Upload an Invoice</h2>
                <p className="mx-auto mt-2 max-w-sm text-sm text-slate-500">
                  Upload PDF/image or use camera to extract invoice and warranty details.
                </p>
                <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
                  <button
                    onClick={() => uploadInputRef.current?.click()}
                    data-gsap-hover="lift"
                    className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 hover:shadow-blue-300/70 transition-all"
                  >
                    <Upload className="h-4 w-4" />
                    Upload File
                  </button>
                  <button
                    onClick={() => cameraInputRef.current?.click()}
                    data-gsap-hover="lift"
                    className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-5 py-2.5 text-sm font-semibold text-slate-700 hover:bg-blue-50 hover:border-blue-200 transition-all"
                  >
                    <Camera className="h-4 w-4" />
                    Camera
                  </button>
                </div>
              </div>
            ) : (
              <div>
                <div className="mb-4 flex items-center justify-between rounded-xl bg-slate-50 border border-slate-100 px-3 py-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-slate-900">{file.name}</p>
                    <p className="text-xs text-slate-400">{(file.size / 1024).toFixed(1)} KB</p>
                  </div>
                  <button
                    onClick={resetScan}
                    className="rounded-lg border border-slate-200 px-2.5 py-1 text-xs font-medium text-slate-600 hover:bg-red-50 hover:text-red-600 hover:border-red-200 transition-colors"
                  >
                    Clear
                  </button>
                </div>

                <div className="mb-4 rounded-xl bg-blue-50 border border-blue-100 p-3">
                  <p className="mb-1 text-xs font-semibold uppercase tracking-[0.2em] text-slate-500">
                    OCR Engine
                  </p>
                  <p className="text-sm font-medium text-blue-700">Google Vision OCR + AWS Bedrock Mapping</p>
                </div>

                {preview && (
                  <div className="relative mb-4 h-[360px] overflow-hidden rounded-xl border border-slate-200 bg-slate-50">
                    <Image src={preview} alt="Invoice preview" fill unoptimized className="object-contain" />
                  </div>
                )}

                {!scanResult && (
                  <button
                    onClick={() => handleScan(false)}
                    disabled={isScanning}
                    data-gsap-hover="lift"
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-4 py-2.5 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 disabled:opacity-70 transition-all"
                  >
                    {isScanning ? (
                      <>
                        <span className="loading loading-spinner loading-sm"></span>
                        Extracting...
                      </>
                    ) : (
                      <>
                        <Zap className="h-4 w-4" />
                        Start Extraction
                      </>
                    )}
                  </button>
                )}
                {showManualFallback && !scanResult && (
                  <div className="mt-3 rounded-xl bg-amber-50 border border-amber-200 p-3">
                    <p className="mb-2 text-xs text-amber-800 font-medium">
                      OCR is unavailable. Add known invoice fields and retry.
                    </p>
                    <div className="grid gap-2 md:grid-cols-2">
                      <input
                        value={manualBillId}
                        onChange={(e) => setManualBillId(e.target.value)}
                        placeholder="Bill ID"
                        className="dashboard-input"
                      />
                      <input
                        value={manualVendor}
                        onChange={(e) => setManualVendor(e.target.value)}
                        placeholder="Vendor"
                        className="dashboard-input"
                      />
                      <input
                        type="date"
                        value={manualPurchaseDate}
                        onChange={(e) => setManualPurchaseDate(e.target.value)}
                        className="dashboard-input"
                      />
                      <input
                        type="number"
                        step="0.01"
                        value={manualTotalAmount}
                        onChange={(e) => setManualTotalAmount(e.target.value)}
                        placeholder="Total Amount"
                        className="dashboard-input"
                      />
                    </div>
                    <button
                      onClick={() => handleScan(true)}
                      disabled={
                        isScanning ||
                        (!manualBillId.trim() &&
                          !manualVendor.trim() &&
                          !manualPurchaseDate.trim() &&
                          !manualTotalAmount.trim())
                      }
                      className="mt-3 inline-flex w-full items-center justify-center rounded-xl bg-amber-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-amber-600 disabled:opacity-70 transition-colors"
                    >
                      {isScanning ? 'Retrying...' : 'Retry With Manual Fields'}
                    </button>
                  </div>
                )}
              </div>
            )}
            <input
              ref={uploadInputRef}
              type="file"
              accept="image/*,.pdf"
              className="hidden"
              onChange={handleFileChange}
            />
            <input
              ref={cameraInputRef}
              type="file"
              accept="image/*"
              capture="environment"
              className="hidden"
              onChange={handleFileChange}
            />
            {error && (
              <div className="mt-4 rounded-xl bg-red-50 border border-red-200 p-3 text-sm text-red-700">
                <div className="inline-flex items-center gap-2">
                  <AlertTriangle className="h-4 w-4" />
                  {error}
                </div>
              </div>
            )}
          </section>

          <section data-gsap="panel" className="dashboard-card p-4">
            {scanResult ? (
              <div>
                <div className="mb-4 inline-flex items-center gap-2 rounded-xl bg-emerald-50 border border-emerald-200 px-3.5 py-2 text-sm font-semibold text-emerald-700">
                  <Check className="h-4 w-4" />
                  Extraction Complete
                </div>

                <div className="space-y-2">
                  <ResultField icon={Package} label="Product" value={item?.productName} />
                  <ResultField icon={Store} label="Seller" value={scanResult.sellerName} />
                  <ResultField icon={Hash} label="Invoice No" value={item?.invoiceNo} />
                  <ResultField icon={DollarSign} label="Amount" value={formatRupee(item?.purchasePrice)} />
                  <ResultField
                    icon={ShieldCheck}
                    label="Warranty"
                    value={item?.warrantyMonths ? `${item.warrantyMonths} months` : null}
                  />
                  <ResultField icon={FileText} label="Category" value={scanResult.category} />
                </div>

                <div className="mt-4 flex flex-wrap gap-2">
                  <button
                    onClick={openDocument}
                    data-gsap-hover="lift"
                    className="inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-blue-600 to-blue-700 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-blue-200/60 transition-all"
                  >
                    <ShieldCheck className="h-4 w-4" />
                    Open Full AI Report
                  </button>
                  <button
                    onClick={resetScan}
                    data-gsap-hover="lift"
                    className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2 text-sm font-semibold text-slate-700 hover:bg-blue-50 hover:border-blue-200 transition-all"
                  >
                    Scan Another
                  </button>
                </div>
                <p className="mt-3 text-xs text-slate-400">
                  Full Bharat AI insights (multilingual summary, claim steps, GST findings, fraud signals, payment refs, voice summary) are available in the document report.
                </p>
              </div>
            ) : (
              <div className="flex h-full min-h-[220px] flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50/50 text-center">
                <ScanLine className="mb-3 h-9 w-9 text-slate-300" />
                <p className="text-sm font-semibold text-slate-700">Awaiting extraction</p>
                <p className="mt-1 max-w-[240px] text-xs text-slate-400">
                  Uploaded invoice details will appear here once extraction completes.
                </p>
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  )
}

function ResultField({
  icon: Icon,
  label,
  value,
}: {
  icon: React.ComponentType<{ className?: string }>
  label: string
  value?: string | number | null
}) {
  return (
    <div className="scan-result-field">
      <div className="icon-box">
        <Icon className="h-4 w-4" />
      </div>
      <div className="min-w-0">
        <p className="text-xs uppercase tracking-[0.12em] text-slate-400 font-medium">{label}</p>
        <p className={`mt-0.5 text-sm ${value ? 'text-slate-900 font-medium' : 'italic text-slate-400'}`}>
          {value || 'Not detected'}
        </p>
      </div>
    </div>
  )
}
