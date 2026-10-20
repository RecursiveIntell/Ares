import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { $goalsBySession, controlSessionGoal } from '@/store/goals'
import { notifyError } from '@/store/notifications'

interface GoalControlsProps {
  sessionId: string
}

export function GoalControls({ sessionId }: GoalControlsProps) {
  const { t } = useI18n()
  const goal = useStore($goalsBySession)[sessionId]
  const [pending, setPending] = useState(false)
  if (!goal || goal.status === 'done') {
    return null
  }
  const run = async (action: 'pause' | 'resume' | 'clear') => {
    setPending(true)
    try {
      await controlSessionGoal(sessionId, action)
    } catch (error) {
      notifyError(error, t.desktop.promptFailed)
    } finally {
      setPending(false)
    }
  }
  const action = goal.autoEligible || goal.status === 'active' || goal.status === 'waiting' ? 'pause' : 'resume'

  return (
    <>
      <Button disabled={pending} onClick={() => void run(action)} size="micro" variant="text">
        {action === 'pause' ? t.statusStack.goalPauseAction : t.statusStack.goalResumeAction}
      </Button>
      <Button disabled={pending} onClick={() => void run('clear')} size="micro" variant="text">
        {t.statusStack.goalClearAction}
      </Button>
    </>
  )
}
