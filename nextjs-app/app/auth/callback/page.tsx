'use client'

import { useEffect, useState } from 'react'
import { useRouter, useSearchParams } from 'next/navigation'
import { Loader2 } from 'lucide-react'

import { getSupabaseBrowserClient } from '@/lib/supabase'
import { persistClientAuthCookies } from '@/lib/auth-client'
import { useAuthStore } from '@/lib/store/auth-store'
import type { UserType } from '@/lib/types'

export default function AuthCallbackPage() {
  const router = useRouter()
  const searchParams = useSearchParams()
  const { setAuth } = useAuthStore()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const handleCallback = async () => {
      try {
        const supabase = getSupabaseBrowserClient()
        const { data: { session }, error: sessionError } = await supabase.auth.getSession()

        if (sessionError || !session?.user) {
          throw new Error(sessionError?.message || 'Authentication failed.')
        }

        const userType = (searchParams.get('userType') as UserType) || 'consumer'
        const user = session.user

        // Check if user profile exists, create if not
        const { data: profile } = await supabase
          .from('user_profiles')
          .select('custom_id, user_type, full_name')
          .eq('user_id', user.id)
          .single()

        let customId = profile?.custom_id
        let resolvedUserType = (profile?.user_type as UserType) || userType

        if (!profile) {
          // First-time OAuth user — create profile
          const prefix = userType === 'merchant' ? 'MER' : 'CON'
          const randomHex = Array.from(crypto.getRandomValues(new Uint8Array(4)))
            .map((b) => b.toString(16).toUpperCase().padStart(2, '0'))
            .join('')
          customId = `${prefix}-${randomHex}`

          await supabase.from('user_profiles').insert({
            user_id: user.id,
            custom_id: customId,
            email: user.email || '',
            full_name: user.user_metadata?.full_name || user.user_metadata?.name || user.email?.split('@')[0] || 'User',
            user_type: userType,
          })
          resolvedUserType = userType
        }

        await setAuth(
          {
            userId: user.id,
            email: user.email,
            name: profile?.full_name || user.user_metadata?.full_name || user.user_metadata?.name || 'User',
            userType: resolvedUserType,
            customId,
            picture: user.user_metadata?.avatar_url,
            provider: user.app_metadata?.provider || 'google',
          },
          session.access_token
        )

        persistClientAuthCookies(session.access_token, resolvedUserType)
        router.push(resolvedUserType === 'merchant' ? '/merchant-dashboard' : '/locker')
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : 'Authentication failed.')
      }
    }

    handleCallback()
  }, [router, searchParams, setAuth])

  if (error) {
    return (
      <div className="min-h-screen bg-base-100 flex items-center justify-center p-4">
        <div className="card bg-base-100 shadow-xl border border-error max-w-md w-full">
          <div className="card-body text-center">
            <h2 className="text-xl font-bold text-error">Authentication Failed</h2>
            <p className="text-base-content/60 text-sm mt-2">{error}</p>
            <button onClick={() => router.push('/login')} className="btn btn-primary mt-4">
              Back to Login
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-screen bg-base-100 flex items-center justify-center">
      <div className="text-center">
        <Loader2 className="w-8 h-8 animate-spin text-primary mx-auto" />
        <p className="text-base-content/60 mt-4">Completing sign in...</p>
      </div>
    </div>
  )
}
