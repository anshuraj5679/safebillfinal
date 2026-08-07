import { getSupabaseBrowserClient } from '@/lib/supabase'
import type { User, UserType } from '@/lib/types'

export interface SignInResult {
  token: string
  user: User
  userType: UserType
}

/**
 * Sign in with email/phone + password via Supabase Auth.
 */
export async function signInWithIdentifier(params: {
  identifier: string
  password: string
  userType: UserType
}): Promise<SignInResult> {
  const supabase = getSupabaseBrowserClient()

  const isEmail = params.identifier.includes('@')

  const { data, error } = isEmail
    ? await supabase.auth.signInWithPassword({
        email: params.identifier.trim(),
        password: params.password,
      })
    : await supabase.auth.signInWithPassword({
        phone: params.identifier.trim(),
        password: params.password,
      })

  if (error || !data.session || !data.user) {
    throw new Error(error?.message || 'Login failed. Please try again.')
  }

  // Fetch user profile to get customId and user type
  const { data: profile } = await supabase
    .from('user_profiles')
    .select('custom_id, user_type, full_name')
    .eq('user_id', data.user.id)
    .single()

  const userType = (profile?.user_type as UserType) || params.userType
  const customId = profile?.custom_id || undefined

  const user: User = {
    userId: data.user.id,
    email: data.user.email,
    phone: data.user.phone || undefined,
    name: profile?.full_name || data.user.user_metadata?.full_name || data.user.email?.split('@')[0] || 'User',
    userType,
    customId,
    provider: data.user.app_metadata?.provider || 'email',
  }

  return {
    token: data.session.access_token,
    user,
    userType,
  }
}

/**
 * Sign up with email/phone + password via Supabase Auth.
 * Creates user_profiles row with custom SafeBill ID.
 */
export async function signUpWithIdentifier(params: {
  identifier: string
  password: string
  name: string
  userType: UserType
}): Promise<{
  user: User
  token: string
  userType: UserType
  customId: string
  needsVerification: boolean
}> {
  const supabase = getSupabaseBrowserClient()
  const isEmail = params.identifier.includes('@')

  // Generate custom SafeBill ID
  const prefix = params.userType === 'merchant' ? 'MER' : 'CON'
  const randomHex = Array.from(crypto.getRandomValues(new Uint8Array(4)))
    .map((b) => b.toString(16).toUpperCase().padStart(2, '0'))
    .join('')
  const customId = `${prefix}-${randomHex}`

  const signUpPayload = isEmail
    ? {
        email: params.identifier.trim(),
        password: params.password,
        options: {
          data: {
            full_name: params.name.trim(),
            user_type: params.userType,
            custom_id: customId,
          },
        },
      }
    : {
        phone: params.identifier.trim(),
        password: params.password,
        options: {
          data: {
            full_name: params.name.trim(),
            user_type: params.userType,
            custom_id: customId,
          },
        },
      }

  const { data, error } = await supabase.auth.signUp(signUpPayload)

  if (error || !data.user) {
    throw new Error(error?.message || 'Signup failed.')
  }

  const needsVerification = !data.session

  // If we have a session (email confirmation disabled), create profile
  if (data.session) {
    await supabase.from('user_profiles').upsert({
      user_id: data.user.id,
      custom_id: customId,
      email: data.user.email || params.identifier.trim(),
      full_name: params.name.trim(),
      user_type: params.userType,
    })
  }

  const user: User = {
    userId: data.user.id,
    email: data.user.email,
    phone: data.user.phone || undefined,
    name: params.name.trim(),
    userType: params.userType,
    customId,
    provider: 'email',
  }

  return {
    user,
    token: data.session?.access_token || '',
    userType: params.userType,
    customId,
    needsVerification,
  }
}

/**
 * Sign in with Google OAuth via Supabase Auth.
 */
export async function signInWithGoogle(userType: UserType) {
  const supabase = getSupabaseBrowserClient()

  const redirectUrl = `${window.location.origin}/auth/callback?userType=${userType}`

  const { error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo: redirectUrl,
      queryParams: {
        access_type: 'offline',
        prompt: 'consent',
      },
    },
  })

  if (error) {
    throw new Error(error.message || 'Google sign-in failed.')
  }
}

/**
 * Request password reset email.
 */
export async function forgotPassword(email: string): Promise<void> {
  const supabase = getSupabaseBrowserClient()
  const { error } = await supabase.auth.resetPasswordForEmail(email.trim(), {
    redirectTo: `${window.location.origin}/auth/reset-password`,
  })
  if (error) {
    throw new Error(error.message || 'Failed to send reset email.')
  }
}

/**
 * Reset password with a new password (called from the reset link).
 */
export async function resetPassword(newPassword: string): Promise<void> {
  const supabase = getSupabaseBrowserClient()
  const { error } = await supabase.auth.updateUser({ password: newPassword })
  if (error) {
    throw new Error(error.message || 'Failed to reset password.')
  }
}

/**
 * Sign out the current user.
 */
export async function signOut(): Promise<void> {
  const supabase = getSupabaseBrowserClient()
  await supabase.auth.signOut()
}

/**
 * Persist user type cookie for middleware route protection.
 */
export function persistClientAuthCookies(token: string, userType: UserType) {
  if (token) {
    document.cookie = `sb_access_token=${token}; path=/; max-age=${60 * 60 * 24 * 7}`
  }
  document.cookie = `sb_user_type=${userType}; path=/; max-age=${60 * 60 * 24 * 7}`
}
